"""End My Day — closing review from stored mail (additive).

Feature flag: END_MY_DAY_ENABLED (default false).

Reads today's stored emails and an existing Evening Wrap-up when one
already exists for the local date. Does not regenerate briefings, sync
Gmail, call OpenAI, send mail, or complete follow-ups.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_FOLLOWUPS = 5
MAX_DEADLINES = 8
MAX_UNRESOLVED = 6
MAX_DECISIONS = 40
DEADLINE_HORIZON_DAYS = 14
REVIEW_CACHE_MINUTES = 18 * 60

# Today's built review, so reopening the panel does not scan mail again.
_REVIEW_CACHE: dict[str, dict] = {}

NO_REPLY_RE = re.compile(
    r"noreply|no-reply|donotreply|newsletter|mailer-daemon|notifications?@",
    re.I,
)
WEBINAR_RE = re.compile(r"\bwebinars?\b", re.I)
PROMO_CATEGORY = {"promotion", "promotions", "newsletter", "marketing", "social"}
# One sync interval of clock skew is enough to tell a stored message from the checkpoint.
SYNC_GAP_MS = 2 * 60 * 1000
_REFRESH_LOCK = threading.Lock()

# One soft word such as "newsletter" is not enough to drop a message.
# A hard signal is bulk-mail or announcement evidence.
_SOFT_BROADCAST_RES = (re.compile(r"\bnewsletter\b", re.I),)
_HARD_BROADCAST_RES = (
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"view (this email|in your browser|as a web page)", re.I),
    re.compile(r"you(?:'re| are) receiving this", re.I),
    re.compile(r"\badmission open\b", re.I),
    re.compile(r"\b(?:register|enroll|enrol) now\b", re.I),
    re.compile(r"\bonline (?:educational )?course\b", re.I),
    re.compile(r"\bcertificate course\b", re.I),
    re.compile(r"\b(?:online )?batch\b", re.I),
    re.compile(r"\bpartner chats\b", re.I),
    re.compile(r"\bcustomer stories\b", re.I),
    re.compile(r"\bwebinars?\b|\bwebcasts?\b", re.I),
    re.compile(r"\binvitation (?:to|for)\b", re.I),
    re.compile(r"\bsummit\b", re.I),
    re.compile(r"\bedition\s*#?\s*\d", re.I),
)
_STRONG_REQUEST_RE = re.compile(
    r"("
    r"\bplease\s+(?:review|send|share|approve|advise|check|sign|finali[sz]e)\b|"
    r"\b(?:could|can|would)\s+you\b|"
    r"\bkindly\s+(?:review|send|share|approve|advise|sign|finali[sz]e)\b|"
    r"\b(?:need|awaiting|requesting)\s+your\b|"
    r"\blet me know\b|"
    r"\byour\s+(?:reply|review|approval|feedback|comments|input)\b|"
    r"\bapprove the\b"
    r")",
    re.I,
)


def end_my_day_enabled() -> bool:
    return os.getenv("END_MY_DAY_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _review_cache_key(account: str, local_date: str) -> str:
    return f"{(account or '').strip().lower()}|{local_date}"


def clear_review_cache() -> None:
    _REVIEW_CACHE.clear()


def store_review_cache(account: str, local_date: str, payload: dict) -> None:
    """Remember today's review in memory and, when available, the catch-up cache table."""
    if not account or not local_date or not isinstance(payload, dict):
        return
    snapshot = dict(payload)
    snapshot["cached"] = True
    _REVIEW_CACHE[_review_cache_key(account, local_date)] = snapshot
    try:
        from db import put_catchup_cache

        put_catchup_cache(
            gmail_account_email=account,
            window_key=f"end-my-day:{local_date}",
            payload=snapshot,
            ttl_minutes=REVIEW_CACHE_MINUTES,
        )
    except Exception:
        logger.info("End My Day review cache was kept in memory only")


def read_review_cache(account: str, local_date: str) -> dict | None:
    key = _review_cache_key(account, local_date)
    cached = _REVIEW_CACHE.get(key)
    if isinstance(cached, dict):
        return dict(cached)
    try:
        from db import get_catchup_cache

        row = get_catchup_cache(
            gmail_account_email=account,
            window_key=f"end-my-day:{local_date}",
        )
    except Exception:
        return None
    payload = (row or {}).get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict) or not payload.get("enabled"):
        return None
    snapshot = dict(payload)
    snapshot["cached"] = True
    _REVIEW_CACHE[key] = snapshot
    return dict(snapshot)


def _uses_live_loaders(
    load_emails,
    load_evening,
    load_states,
    load_sync,
    detect_followups,
    load_checkpoint,
) -> bool:
    return all(
        loader is None
        for loader in (
            load_emails,
            load_evening,
            load_states,
            load_sync,
            detect_followups,
            load_checkpoint,
        )
    )


def checkpoint_table_missing(exc: Exception) -> bool:
    message = str(exc)
    return "end_my_day_checkpoints" in message or "PGRST205" in message


def _tz():
    from briefing_service import get_briefing_tz

    return get_briefing_tz()


def _analysis(email: dict) -> dict:
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return analyses[0] if analyses else {}
    return analyses if isinstance(analyses, dict) else {}


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def today_window(now: datetime | None = None) -> dict[str, Any]:
    """Local calendar day so far, using the briefing timezone."""
    from briefing_modes import compute_mode_period
    from briefing_service import get_briefing_timezone_name

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    period = compute_mode_period("evening", now=current, tz=_tz())
    local_date = period["anchor_date"]
    return {
        "local_date": local_date.isoformat(),
        "anchor_date": local_date,
        "start_ms": period["start_ms"],
        "end_ms": period["end_ms"],
        "start_local": period["local_start"].isoformat(),
        "end_local": period["local_end"].isoformat(),
        "timezone": get_briefing_timezone_name(),
    }


def suppressed_recommendation_keys(
    states: list[dict] | None,
    *,
    now: datetime | None = None,
) -> set[str]:
    """Dismissed, completed, and still-snoozed keys. Expired snoozes stay visible."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    hidden: set[str] = set()
    for row in states or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("recommendation_key") or "").strip()
        if not key:
            continue
        status = str(row.get("status") or "").strip().lower()
        if status in {"dismissed", "completed"}:
            hidden.add(key)
            continue
        if status != "snoozed":
            continue
        until = _parse_iso(str(row.get("snoozed_until") or ""))
        if until and until > current:
            hidden.add(key)
    return hidden


def is_excluded_mail(email: dict | None, *, direction: str = "") -> bool:
    """Promotional, webinar, and generic informational mail."""
    if not isinstance(email, dict):
        return False
    analysis = _analysis(email)
    sender = str(email.get("sender") or "")
    subject = str(email.get("subject") or "")
    title = str(email.get("title") or "")
    blob = " ".join(
        [
            subject,
            title,
            str(email.get("snippet") or ""),
            str(analysis.get("summary") or ""),
        ]
    )
    labels = email.get("label_ids") or []
    category = str(analysis.get("category") or "").strip().lower()
    if NO_REPLY_RE.search(sender) or category in PROMO_CATEGORY:
        return True
    if isinstance(labels, list) and any(
        str(label).upper() == "CATEGORY_PROMOTIONS" for label in labels
    ):
        return True
    if WEBINAR_RE.search(subject) or WEBINAR_RE.search(title) or WEBINAR_RE.search(blob):
        return True
    if direction == "outgoing":
        from ai_followup_assistant import looks_like_informational_outgoing

        if looks_like_informational_outgoing(email, analysis):
            return True
    action = analysis.get("action_required")
    importance = str(analysis.get("importance") or "").strip().lower()
    if action is False and (importance == "low" or category in {"other", "updates", "update"}):
        return True
    if action is not True and any(
        marker in blob.lower()
        for marker in ("no action needed", "no reply needed", "for your information", "fyi")
    ):
        return True
    return False


def incremental_stale_minutes() -> int:
    """Age after which an incremental Gmail sync is stale for End My Day.

    END_MY_DAY_SYNC_STALE_MINUTES overrides the threshold.
    Otherwise this reuses GMAIL_SYNC_INTERVAL_MINUTES (default 15), the same
    interval the existing scheduler already uses. Historical backfill is a
    separate flag and does not use this clock.
    """
    raw = os.getenv("END_MY_DAY_SYNC_STALE_MINUTES", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            logger.warning("Invalid END_MY_DAY_SYNC_STALE_MINUTES=%r", raw)
    try:
        from scheduler import _sync_interval_minutes

        return max(1, int(_sync_interval_minutes()))
    except Exception:
        return 15


def _evidence_text(email: dict, analysis: dict) -> str:
    return " ".join(
        str(email.get(key) or "")
        for key in ("subject", "title", "snippet", "body")
    ) + " " + str(analysis.get("summary") or "")


def _pattern_hits(text: str, patterns: tuple) -> int:
    return sum(1 for pattern in patterns if pattern.search(text))


def _classification_hits(email: dict, analysis: dict) -> int:
    hits = 0
    sender = str(email.get("sender") or "")
    category = str(analysis.get("category") or "").strip().lower()
    labels = email.get("label_ids") or []
    if NO_REPLY_RE.search(sender):
        hits += 1
    if category in PROMO_CATEGORY:
        hits += 1
    if isinstance(labels, list) and any(
        str(label).upper() in {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}
        for label in labels
    ):
        hits += 1
    return hits


def classify_reply_need(email: dict | None, analysis: dict | None = None) -> str:
    """Whether a stored message is a confirmed reply, ambiguous, or not a request.

    Importance and action_required are not enough. A confirmed reply needs a
    directed request in the message or its saved summary, and no bulk-mail
    evidence. One broadcast phrase alone does not drop the message; it becomes
    Needs review. Two content signals, or one content signal plus a label,
    category, or no-reply sender, exclude it when nobody asked the user directly.

    Briefing follow-ups keep their own state machine. End My Day applies this
    gate before showing "awaiting you".
    """
    if not isinstance(email, dict):
        return "not_a_request"
    parsed = analysis if isinstance(analysis, dict) else _analysis(email)
    text = _evidence_text(email, parsed)
    strong = bool(_STRONG_REQUEST_RE.search(text))
    hard_hits = _pattern_hits(text, _HARD_BROADCAST_RES)
    soft_hits = _pattern_hits(text, _SOFT_BROADCAST_RES)
    class_hits = _classification_hits(email, parsed)
    action = parsed.get("action_required")
    # A generic "please confirm" or "kindly reply" inside an announcement is not
    # a request directed at this user. Only a work request promotes the message.
    if strong and hard_hits == 0 and soft_hits == 0 and class_hits == 0:
        return "confirmed_request"
    if hard_hits >= 2 or (hard_hits >= 1 and not strong) or class_hits >= 1 or soft_hits >= 2:
        return "not_a_request"
    if strong or soft_hits == 1 or action is True:
        return "needs_review"
    return "not_a_request"


def _epoch_ms(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    if number < 10_000_000_000:
        return number * 1000
    return number


def _analysis_pending(email: dict) -> bool:
    status = str(email.get("analysis_status") or "").strip().lower()
    if status in {"pending", "failed", "processing"}:
        return True
    if status == "done":
        return False
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return len(analyses) == 0
    return not isinstance(analyses, dict)


def _thread_has_later_reply(source: dict, messages: list[dict], account: str) -> bool:
    """True when the user already sent a later message on this thread."""
    from briefing_followup import message_direction

    source_id = str(source.get("id") or "")
    ordered = sorted(
        [msg for msg in messages if isinstance(msg, dict)],
        key=lambda msg: (_epoch_ms(msg.get("internal_date")), str(msg.get("id") or "")),
    )
    seen = False
    for msg in ordered:
        if str(msg.get("id") or "") == source_id:
            seen = True
            continue
        if not seen:
            continue
        if message_direction(msg, account) == "outgoing":
            return True
    return False


def _without_answered(
    rows: list[dict],
    *,
    account: str,
    emails_by_id: dict[str, dict],
    load_threads: Callable[..., list] | None,
    limit: int = 8,
    answered_ids: set[str] | None = None,
) -> list[dict]:
    if load_threads is None:
        return rows
    cache: dict[str, list | None] = {}
    checked = 0
    kept: list[dict] = []
    for row in rows:
        thread_id = str(row.get("gmail_thread_id") or "").strip()
        source_id = str(row.get("source_email_id") or "")
        if not thread_id or thread_id.startswith("msg:") or checked >= limit:
            kept.append(row)
            continue
        if thread_id not in cache:
            checked += 1
            try:
                cache[thread_id] = list(
                    load_threads(gmail_account_email=account, thread_id=thread_id) or []
                )
            except Exception:
                logger.info("End My Day skipped a thread reply check")
                cache[thread_id] = None
        messages = cache[thread_id]
        source = emails_by_id.get(source_id) or {"id": source_id}
        if messages and _thread_has_later_reply(source, messages, account):
            if answered_ids is not None and source_id:
                answered_ids.add(source_id)
            continue
        kept.append(row)
    return kept


def _email_index(emails: list[dict]) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for email in emails:
        email_id = str(email.get("id") or "")
        if email_id:
            indexed[email_id] = email
    return indexed


def _ref(row: dict, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_email_id": row.get("source_email_id") or None,
        "gmail_thread_id": row.get("gmail_thread_id") or None,
        "recommendation_key": row.get("recommendation_key") or None,
        "title": str(row.get("title") or row.get("subject") or "")[:180],
    }


def collect_deadlines(emails: list[dict], *, today: date) -> list[dict]:
    """Upcoming deadlines through the horizon, plus overdue dates."""
    from briefing_structure import (
        PERSONAL_DEADLINE_RE,
        PROMO_DEADLINE_RE,
        parse_due_date,
        sender_display,
    )

    horizon = today + timedelta(days=DEADLINE_HORIZON_DAYS)
    found: list[dict] = []
    seen: set[str] = set()
    for email in emails:
        if is_excluded_mail(email):
            continue
        analysis = _analysis(email)
        sender = str(email.get("sender") or "")
        subject = str(email.get("subject") or "").strip()
        for raw in analysis.get("deadlines") or []:
            due = parse_due_date(str(raw))
            if due is None or due > horizon:
                continue
            evidence = f"{sender}\n{subject}\n{raw}"
            if PROMO_DEADLINE_RE.search(evidence) and not PERSONAL_DEADLINE_RE.search(
                evidence
            ):
                continue
            key = f"{due.isoformat()}|{email.get('id')}|{subject}"
            if key in seen:
                continue
            seen.add(key)
            raw_title = str(raw).strip()
            title = subject[:120] if parse_due_date(raw_title) and subject else raw_title[:160]
            found.append(
                {
                    "title": title or subject or "Deadline",
                    "due_date": due.isoformat(),
                    "overdue": due < today,
                    "priority": (
                        str(analysis.get("importance") or "medium").lower()
                        if str(analysis.get("importance") or "").lower()
                        in {"high", "medium", "low"}
                        else "medium"
                    ),
                    "source_email_id": email.get("id") or "",
                    "gmail_thread_id": email.get("gmail_thread_id"),
                    "sender": sender_display(sender),
                    "subject": subject,
                }
            )
    found.sort(key=lambda row: (str(row.get("due_date") or "9999"), str(row.get("title"))))
    return found[:MAX_DEADLINES]


def _followup_rows(
    active: list[dict],
    *,
    emails_by_id: dict[str, dict],
    suppressed: set[str],
) -> dict[str, list[dict]]:
    from ai_followup_assistant import followup_actions_enabled
    from briefing_structure import sender_display

    actions_on = followup_actions_enabled()
    incoming: list[dict] = []
    outgoing: list[dict] = []
    needs_review: list[dict] = []
    for item in active:
        if not isinstance(item, dict):
            continue
        key = str(item.get("recommendation_key") or "").strip()
        if key and key in suppressed:
            continue
        source_id = str(item.get("source_email_id") or "")
        email = emails_by_id.get(source_id) or {}
        state = str(item.get("followup_state") or "")
        direction = "outgoing" if state == "awaiting_response" else "incoming"
        if is_excluded_mail(email or item, direction=direction):
            continue
        if WEBINAR_RE.search(str(item.get("title") or "")) or WEBINAR_RE.search(
            str(item.get("subject") or "")
        ):
            continue
        row = {
            "recommendation_key": key or None,
            "source_email_id": source_id or None,
            "gmail_thread_id": item.get("gmail_thread_id"),
            "subject": item.get("subject"),
            "title": item.get("title") or item.get("subject"),
            "sender": sender_display(str(item.get("sender") or "")) or item.get("sender"),
            "recipient": sender_display(str(item.get("recipient") or "")) or None,
            "priority": item.get("priority"),
            "waiting_hours": item.get("waiting_hours") or 0,
            "followup_state": state,
            "actions_enabled": bool(actions_on and key),
        }
        if state == "pending_reply":
            need = classify_reply_need(email or item, _analysis(email or item))
            if need == "not_a_request":
                continue
            if need == "needs_review":
                row["review_status"] = "needs_review"
                row["title"] = item.get("subject") or item.get("title")
                needs_review.append(row)
                continue
            incoming.append(row)
        elif state == "awaiting_response":
            outgoing.append(row)
    return {
        "incoming_awaiting_user": incoming[:MAX_FOLLOWUPS],
        "outgoing_awaiting_recipient": outgoing[:MAX_FOLLOWUPS],
        "needs_review": needs_review[:MAX_UNRESOLVED],
    }


def _unresolved_items(
    emails: list[dict],
    *,
    account: str,
    used_ids: set[str],
    suppressed_source_ids: set[str],
) -> list[dict]:
    from briefing_followup import message_direction
    from briefing_structure import sender_display

    rows: list[dict] = []
    review_rows: list[dict] = []
    for email in emails:
        email_id = str(email.get("id") or "")
        if not email_id or email_id in used_ids or email_id in suppressed_source_ids:
            continue
        if message_direction(email, account) == "outgoing":
            continue
        if is_excluded_mail(email, direction="incoming"):
            continue
        analysis = _analysis(email)
        importance = str(analysis.get("importance") or "").lower()
        need = classify_reply_need(email, analysis)
        if need == "not_a_request":
            continue
        item = {
            "source_email_id": email_id,
            "gmail_thread_id": email.get("gmail_thread_id"),
            "title": str(email.get("subject") or "Email")[:140],
            "subject": email.get("subject"),
            "sender": sender_display(email.get("sender")),
            "reason": str(analysis.get("summary") or email.get("snippet") or "")[:240],
            "priority": importance if importance in {"high", "medium"} else "medium",
        }
        if need == "needs_review":
            item["review_status"] = "needs_review"
            review_rows.append(item)
            continue
        if analysis.get("action_required") is not True or importance == "low":
            continue
        rows.append(item)
    return {"unresolved": rows[:MAX_UNRESOLVED], "needs_review": review_rows[:MAX_UNRESOLVED]}


def _sync_freshness(
    sync_state: dict | None,
    emails: list[dict] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Separate incremental sync freshness from historical backfill.

    Inbox and End My Day both read stored emails. The sync checkpoint
    (gmail_sync_state.updated_at) is a different clock from each message's
    Gmail internal_date. A stored message newer than that checkpoint means
    today's window is not verified, even when the Inbox already lists it.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    threshold = incremental_stale_minutes()
    state = sync_state or {}
    backfill = str(state.get("backfill_status") or "").strip().lower()
    historical_complete = backfill in {"complete", "done"}
    updated = state.get("updated_at") or state.get("last_sync_at")
    when = _parse_iso(str(updated)) if updated else None
    newest_ms = max((_epoch_ms(email.get("internal_date")) for email in emails or []), default=0)
    sync_ms = int(when.timestamp() * 1000) if when else 0
    age_min = ((current - when).total_seconds() / 60) if when else None
    incremental_fresh = age_min is not None and age_min <= threshold
    checkpoint_behind = bool(sync_ms and newest_ms > sync_ms + SYNC_GAP_MS)
    analysis_pending = sum(1 for email in emails or [] if _analysis_pending(email))
    window_covered = bool(incremental_fresh and not checkpoint_behind and analysis_pending == 0)
    finish_allowed = bool(when and window_covered)
    parts: list[str] = []
    if not when:
        parts.append("Gmail sync state is unavailable, so today's mailbox coverage cannot be verified.")
    elif not incremental_fresh:
        parts.append(
            f"Last incremental sync is older than the {threshold}-minute sync interval. "
            "Today's review may miss newer mail."
        )
    elif checkpoint_behind:
        parts.append(
            "Stored mail is newer than the last sync checkpoint, so today's window is not verified."
        )
    else:
        parts.append("A recent incremental sync covers today's stored mail.")
    if analysis_pending:
        parts.append(
            f"{analysis_pending} of today's messages still need analysis, so the review is incomplete."
        )
    if historical_complete:
        parts.append("Historical mailbox backfill is complete.")
    elif backfill:
        parts.append(
            "Historical mailbox backfill is still incomplete, so older mail may be missing. "
            "The mailbox is not fully synchronized."
        )
    else:
        parts.append(
            "Historical backfill status is unknown. The mailbox is not fully synchronized."
        )
    if not finish_allowed:
        parts.append("Finish My Day stays unavailable until today's window can be verified.")
    coverage = "cached_only"
    if when and incremental_fresh and not checkpoint_behind:
        coverage = "recent_sync" if historical_complete else "recent_sync_partial_backfill"
    elif when:
        coverage = "possibly_stale"
    if backfill and not historical_complete and coverage == "recent_sync_partial_backfill":
        coverage = "recent_sync_partial_backfill"
    elif backfill and not historical_complete and not incremental_fresh:
        coverage = "partial_backfill"
    return {
        "last_sync_at": updated,
        "newest_stored_at": (
            datetime.fromtimestamp(newest_ms / 1000, tz=timezone.utc).isoformat()
            if newest_ms
            else None
        ),
        "stale": not incremental_fresh,
        "incremental_fresh": incremental_fresh,
        "checkpoint_behind_stored_mail": checkpoint_behind,
        "historical_backfill_complete": historical_complete,
        "backfill_status": state.get("backfill_status"),
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "stale_after_minutes": threshold,
        "analysis_pending": analysis_pending,
        "window_covered": window_covered,
        "finish_allowed": finish_allowed,
        "finish_block_reason": "" if finish_allowed else " ".join(parts),
        "coverage": coverage,
        "complete": bool(finish_allowed and historical_complete),
        "message": " ".join(parts),
    }


def _wrapup_narrative(run: dict | None, *, local_date: str) -> tuple[str, str]:
    if not isinstance(run, dict):
        return "", "stored_mail"
    if str(run.get("period_key") or "") != local_date:
        return "", "stored_mail"
    content = run.get("content") if isinstance(run.get("content"), dict) else {}
    headline = str(content.get("headline") or "").strip()
    highlights = content.get("highlights") or []
    lines = [headline] if headline else []
    if isinstance(highlights, list):
        for item in highlights[:6]:
            text = str(item).strip()
            if text:
                lines.append(text)
    narrative = " ".join(lines).strip()
    if not narrative:
        return "", "stored_mail"
    return narrative[:800], "evening_wrapup"


def _count_sentence(
    *,
    incoming_count: int,
    incoming_waiting: int,
    outgoing_waiting: int,
    deadlines: int,
    unresolved: int,
) -> str:
    mail = "email" if incoming_count == 1 else "emails"
    deadline_word = "deadline" if deadlines == 1 else "deadlines"
    return (
        f"{incoming_count} incoming {mail} stored today. "
        f"{incoming_waiting} awaiting your reply, "
        f"{outgoing_waiting} awaiting a recipient, "
        f"{deadlines} upcoming or overdue {deadline_word}, "
        f"and {unresolved} other unresolved items. "
        "Counts use stored mail only."
    )


def _sanitize_decisions(decisions: list | None) -> list[dict]:
    cleaned: list[dict] = []
    allowed = {"draft_reply", "draft_followup", "remind_tomorrow", "dismiss", "view_email"}
    for row in decisions or []:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in allowed:
            continue
        cleaned.append(
            {
                "action": action,
                "recommendation_key": str(row.get("recommendation_key") or "")[:200] or None,
                "source_email_id": str(row.get("source_email_id") or "")[:80] or None,
                "at": str(row.get("at") or "")[:40] or None,
            }
        )
        if len(cleaned) >= MAX_DECISIONS:
            break
    return cleaned


def build_review(
    *,
    account_email: str,
    now: datetime | None = None,
    force_refresh: bool = False,
    load_emails: Callable[..., list] | None = None,
    load_evening: Callable[..., dict | None] | None = None,
    load_states: Callable[..., list] | None = None,
    load_sync: Callable[..., dict | None] | None = None,
    detect_followups: Callable[..., dict] | None = None,
    load_checkpoint: Callable[..., dict | None] | None = None,
    load_threads: Callable[..., list] | None = None,
) -> dict[str, Any]:
    if not end_my_day_enabled():
        return {"enabled": False, "message": "End My Day is disabled."}
    account = (account_email or "").strip()
    if not account:
        raise PermissionError("missing_account")

    window = today_window(now)
    live = _uses_live_loaders(
        load_emails,
        load_evening,
        load_states,
        load_sync,
        detect_followups,
        load_checkpoint,
    )
    if live and not force_refresh:
        cached = read_review_cache(account, window["local_date"])
        if cached:
            return cached
    if load_emails is None:
        from db import list_emails_for_briefing_range as load_emails
    emails = list(
        load_emails(
            gmail_account_email=account,
            start_ms=window["start_ms"],
            end_ms=window["end_ms"],
            limit=200,
            include_body=True,
        )
        or []
    )
    if load_evening is None:
        from briefing_modes import get_mode_latest

        def load_evening(mode: str, account_email: str) -> dict | None:
            return get_mode_latest(mode, account_email)

    evening = load_evening("evening", account)
    if load_states is None:
        from db import list_recommendation_states_for_account as load_states
    states = list(load_states(gmail_account_email=account) or [])
    if load_sync is None:
        from db import get_sync_state as load_sync
    sync_state = load_sync(account)
    if detect_followups is None:
        from briefing_followup import detect_followups_for_account_sync

        def detect_followups(rows: list, *, account_email: str) -> dict:
            # Classify from the messages already loaded for today. Thread
            # reads happen only for the short reply-candidate list below.
            return detect_followups_for_account_sync(
                rows,
                account_email=account_email,
                gmail_account_email=account_email,
                list_thread_messages=lambda **_kwargs: [],
            )

    detected = detect_followups(emails, account_email=account) or {}
    suppressed = suppressed_recommendation_keys(states, now=now)
    indexed = _email_index(emails)
    waiting = _followup_rows(
        list(detected.get("active") or []),
        emails_by_id=indexed,
        suppressed=suppressed,
    )
    if load_threads is None and live:
        from db import list_thread_messages

        def load_threads(*, gmail_account_email: str, thread_id: str) -> list:
            return list_thread_messages(
                gmail_account_email=gmail_account_email,
                thread_id=thread_id,
            )

    thread_loader = load_threads
    answered_ids: set[str] = set()
    waiting["incoming_awaiting_user"] = _without_answered(
        waiting["incoming_awaiting_user"],
        account=account,
        emails_by_id=indexed,
        load_threads=thread_loader,
        answered_ids=answered_ids,
    )
    waiting["needs_review"] = _without_answered(
        waiting.get("needs_review") or [],
        account=account,
        emails_by_id=indexed,
        load_threads=thread_loader,
        answered_ids=answered_ids,
    )
    today = window["anchor_date"]
    deadlines = collect_deadlines(emails, today=today)
    used_ids = {
        str(row.get("source_email_id") or "")
        for row in (
            waiting["incoming_awaiting_user"]
            + waiting["outgoing_awaiting_recipient"]
            + (waiting.get("needs_review") or [])
        )
        if row.get("source_email_id")
    }
    used_ids.update(answered_ids)
    suppressed_sources = {
        str(row.get("source_id") or "")
        for row in states
        if str(row.get("recommendation_key") or "") in suppressed and row.get("source_id")
    }
    grouped = _unresolved_items(
        emails,
        account=account,
        used_ids=used_ids,
        suppressed_source_ids=suppressed_sources,
    )
    unresolved = grouped["unresolved"]
    needs_review = (waiting.get("needs_review") or []) + grouped["needs_review"]
    seen_review: set[str] = set()
    deduped_review: list[dict] = []
    for row in needs_review:
        source_id = str(row.get("source_email_id") or "")
        if source_id and source_id in seen_review:
            continue
        if source_id:
            seen_review.add(source_id)
        deduped_review.append(row)
    needs_review = _without_answered(
        deduped_review[:MAX_UNRESOLVED],
        account=account,
        emails_by_id=indexed,
        load_threads=thread_loader,
        answered_ids=answered_ids,
    )
    from briefing_followup import message_direction

    incoming_count = sum(
        1 for email in emails if message_direction(email, account) != "outgoing"
    )
    counts = _count_sentence(
        incoming_count=incoming_count,
        incoming_waiting=len(waiting["incoming_awaiting_user"]),
        outgoing_waiting=len(waiting["outgoing_awaiting_recipient"]),
        deadlines=len(deadlines),
        unresolved=len(unresolved),
    )
    narrative, source = _wrapup_narrative(evening, local_date=window["local_date"])
    summary = f"{narrative} {counts}".strip() if narrative else counts

    checkpoint = None
    checkpoint_note = None
    if load_checkpoint is None:
        from db import get_end_my_day_checkpoint as load_checkpoint
    try:
        checkpoint = load_checkpoint(
            gmail_account_email=account, local_date=window["local_date"]
        )
    except Exception as exc:
        if checkpoint_table_missing(exc):
            checkpoint_note = (
                "Closing checkpoint storage is unavailable until "
                "docs/migrations/2026-11-05_end_my_day_checkpoints.sql is applied."
            )
        else:
            logger.exception("End My Day checkpoint read failed")
            checkpoint_note = "Saved checkpoint could not be loaded."

    payload = {
        "enabled": True,
        "local_date": window["local_date"],
        "timezone": window["timezone"],
        "window": {
            "start_local": window["start_local"],
            "end_local": window["end_local"],
            "timezone": window["timezone"],
        },
        "wrapup_source": source,
        "summary_text": summary,
        "verified_counts": counts,
        "metrics": {
            "emails_received": incoming_count,
            "incoming_awaiting_user": len(waiting["incoming_awaiting_user"]),
            "outgoing_awaiting_recipient": len(waiting["outgoing_awaiting_recipient"]),
            "needs_review": len(needs_review),
            "deadlines": len(deadlines),
            "unresolved": len(unresolved),
        },
        "incoming_awaiting_user": waiting["incoming_awaiting_user"],
        "outgoing_awaiting_recipient": waiting["outgoing_awaiting_recipient"],
        "needs_review": needs_review,
        "deadlines": deadlines,
        "unresolved_items": unresolved,
        "data_freshness": _sync_freshness(
            sync_state if isinstance(sync_state, dict) else None,
            emails,
            now=now,
        ),
        "checkpoint": checkpoint,
        "checkpoint_note": checkpoint_note,
        "cached": False,
    }
    if live:
        store_review_cache(account, window["local_date"], payload)
    return payload


def _item_refs(review: dict) -> tuple[list[dict], list[dict]]:
    refs: list[dict] = []
    unresolved: list[dict] = []
    for kind, key in (
        ("incoming", "incoming_awaiting_user"),
        ("needs_review", "needs_review"),
        ("outgoing", "outgoing_awaiting_recipient"),
        ("deadline", "deadlines"),
        ("unresolved", "unresolved_items"),
    ):
        for row in review.get(key) or []:
            if not isinstance(row, dict):
                continue
            ref = _ref(row, kind)
            refs.append(ref)
            if kind != "deadline" or row.get("overdue"):
                unresolved.append(ref)
            elif kind == "deadline":
                unresolved.append(ref)
    return refs[:40], unresolved[:40]


def finish_my_day(
    *,
    account_email: str,
    decisions: list | None = None,
    now: datetime | None = None,
    review: dict | None = None,
    upsert_checkpoint: Callable[..., dict] | None = None,
) -> dict[str, Any]:
    """Save today's closing checkpoint. Does not complete recommendations."""
    if not end_my_day_enabled():
        return {"enabled": False, "message": "End My Day is disabled."}
    account = (account_email or "").strip()
    if not account:
        raise PermissionError("missing_account")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    built = review if isinstance(review, dict) and review.get("enabled") else build_review(
        account_email=account, now=current
    )
    if not built.get("enabled"):
        return built
    freshness = built.get("data_freshness") if isinstance(built.get("data_freshness"), dict) else {}
    if freshness.get("finish_allowed") is not True:
        reason = str(
            freshness.get("finish_block_reason")
            or freshness.get("message")
            or "Today's mailbox coverage is not verified, so the closing checkpoint was not saved."
        )
        return {
            "enabled": True,
            "ok": False,
            "saved": False,
            "checkpoint": None,
            "message": reason,
            "data_freshness": freshness,
        }
    item_refs, unresolved = _item_refs(built)
    if upsert_checkpoint is None:
        from db import upsert_end_my_day_checkpoint as upsert_checkpoint
    try:
        row = upsert_checkpoint(
            gmail_account_email=account,
            local_date=str(built["local_date"]),
            closed_at=current.astimezone(timezone.utc).isoformat(),
            timezone_name=str(built.get("timezone") or ""),
            summary=str(built.get("summary_text") or "")[:1200],
            wrapup_source=str(built.get("wrapup_source") or "stored_mail"),
            item_refs=item_refs,
            decisions=_sanitize_decisions(decisions),
            unresolved=unresolved,
        )
    except Exception as exc:
        if checkpoint_table_missing(exc):
            raise RuntimeError(
                "end_my_day_checkpoints table missing. Run "
                "docs/migrations/2026-11-05_end_my_day_checkpoints.sql when approved."
            ) from exc
        raise
    saved = dict(built)
    saved["checkpoint"] = row
    saved["cached"] = True
    store_review_cache(account, str(built.get("local_date") or ""), saved)
    return {
        "enabled": True,
        "ok": True,
        "saved": True,
        "checkpoint": row,
        "unresolved_count": len(unresolved),
        "message": "Today's review was saved.",
    }


def refresh_mailbox(
    *,
    account_email: str,
    now: datetime | None = None,
    run_sync: Callable[[], dict] | None = None,
    analyze_pending: Callable[..., dict] | None = None,
    count_pending: Callable[..., int] | None = None,
    syncing: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run the existing Gmail sync, wait for today's analysis, then rebuild the review.

    Does not start a second scheduler and does not send mail. A refresh already
    in progress returns without calling sync again.
    """
    if not end_my_day_enabled():
        return {"enabled": False, "ok": False, "status": "failed", "message": "End My Day is disabled."}
    account = (account_email or "").strip()
    if not account:
        raise PermissionError("missing_account")
    if not _REFRESH_LOCK.acquire(blocking=False):
        return {
            "enabled": True,
            "ok": False,
            "status": "already_running",
            "review": None,
            "message": "A mailbox refresh is already running.",
        }
    try:
        if syncing is None:
            from gmail_oauth import get_gmail_auth_snapshot

            def syncing() -> bool:
                return bool(get_gmail_auth_snapshot().get("syncing"))

        if syncing():
            return {
                "enabled": True,
                "ok": False,
                "status": "already_running",
                "review": None,
                "message": "A mailbox refresh is already running.",
            }
        if run_sync is None:
            allowed = os.getenv("ENABLE_MANUAL_SYNC", "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if not allowed:
                return {
                    "enabled": True,
                    "ok": False,
                    "status": "failed",
                    "review": None,
                    "message": (
                        "Manual inbox sync is disabled. Set ENABLE_MANUAL_SYNC=true "
                        "to refresh the mailbox."
                    ),
                }
            from scheduler import run_gmail_sync_once as run_sync
        try:
            stats = run_sync() or {}
        except Exception:
            logger.exception("End My Day mailbox refresh failed")
            return {
                "enabled": True,
                "ok": False,
                "status": "failed",
                "review": None,
                "message": "Mailbox refresh failed. The closing review was not updated.",
            }
        status = str(stats.get("status") or "")
        if status in {"auth_unavailable", "failed", "error"} or stats.get("error"):
            return {
                "enabled": True,
                "ok": False,
                "status": "failed",
                "review": None,
                "message": str(
                    stats.get("error")
                    or "Mailbox refresh failed. The closing review was not updated."
                ),
            }
        window = today_window(now)
        if count_pending is None:
            from db import list_pending_analysis_emails

            def count_pending(*, gmail_account_email: str, start_ms: int) -> int:
                rows = list_pending_analysis_emails(
                    gmail_account_email=gmail_account_email,
                    limit=30,
                    statuses=["pending", "failed"],
                    min_internal_date_ms=start_ms,
                )
                return len(rows or [])

        if analyze_pending is None:
            from scheduler import analyze_pending_emails as analyze_pending

        try:
            remaining = int(
                count_pending(gmail_account_email=account, start_ms=window["start_ms"]) or 0
            )
            passes = 0
            while remaining and passes < 3:
                analyze_pending(
                    gmail_account_email=account,
                    limit=30,
                    statuses=["pending", "failed"],
                )
                passes += 1
                remaining = int(
                    count_pending(gmail_account_email=account, start_ms=window["start_ms"]) or 0
                )
        except Exception:
            logger.exception("End My Day refresh could not finish analysis")
            return {
                "enabled": True,
                "ok": False,
                "status": "failed",
                "review": None,
                "message": "Mailbox sync finished, but analysis did not. The review was not marked updated.",
            }
        review = build_review(account_email=account, now=now, force_refresh=True)
        freshness = review.get("data_freshness") if isinstance(review.get("data_freshness"), dict) else {}
        if freshness.get("finish_allowed") and freshness.get("historical_backfill_complete"):
            return {
                "enabled": True,
                "ok": True,
                "status": "success",
                "review": review,
                "message": "Mailbox refreshed. Today's review is updated.",
            }
        return {
            "enabled": True,
            "ok": True,
            "status": "partial",
            "review": review,
            "message": str(
                freshness.get("message")
                or "Refresh finished, but today's mailbox coverage is still incomplete."
            ),
        }
    finally:
        _REFRESH_LOCK.release()
