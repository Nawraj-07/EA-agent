"""Daily email briefing generation (Task 6.2) + history helpers."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from briefing_structure import (
    BriefingContent,
    extract_analysis_deadlines,
    hydrate_briefing_payload,
    looks_eventish,
    parse_calendar_invite,
    received_iso,
)
from db import (
    list_briefings_since,
    list_emails_for_briefing,
    list_emails_for_briefing_day,
    list_stored_emails,
    save_briefing,
)

logger = logging.getLogger(__name__)

DEFAULT_BRIEFING_TIMEZONE = "Asia/Kolkata"
HISTORY_PRIOR_DAYS = 7  # today + 7 prior = 8 calendar days
EVENT_LOOKBACK_DAYS = 14


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def get_briefing_timezone_name() -> str:
    return (
        os.getenv("BRIEFING_TIMEZONE", DEFAULT_BRIEFING_TIMEZONE).strip()
        or DEFAULT_BRIEFING_TIMEZONE
    )


def get_briefing_tz() -> ZoneInfo:
    name = get_briefing_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning(
            "Invalid BRIEFING_TIMEZONE=%r; falling back to %s",
            name,
            DEFAULT_BRIEFING_TIMEZONE,
        )
        return ZoneInfo(DEFAULT_BRIEFING_TIMEZONE)


def local_date_for(dt: datetime, tz: ZoneInfo | None = None) -> date:
    zone = tz or get_briefing_tz()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(zone).date()


def parse_created_at(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def history_date_range(today_local: date | None = None) -> list[date]:
    """Oldest → newest: today-7 … today (8 days)."""
    zone = get_briefing_tz()
    today = today_local or datetime.now(timezone.utc).astimezone(zone).date()
    start = today - timedelta(days=HISTORY_PRIOR_DAYS)
    return [start + timedelta(days=offset) for offset in range(HISTORY_PRIOR_DAYS + 1)]


def date_iso_to_internal_ms_range(
    date_from_iso: str | None,
    date_to_iso: str | None,
    tz: ZoneInfo | None = None,
) -> tuple[int | None, int | None]:
    """
    Inclusive local calendar days → Gmail internal_date ms bounds.

    start_ms: midnight at start of date_from (inclusive)
    end_ms: midnight at start of day after date_to (exclusive)
    """
    zone = tz or get_briefing_tz()
    start_ms = end_ms = None
    if date_from_iso:
        try:
            d0 = date.fromisoformat(date_from_iso[:10])
            start = datetime.combine(d0, datetime.min.time(), tzinfo=zone)
            start_ms = int(start.timestamp() * 1000)
        except ValueError:
            pass
    if date_to_iso:
        try:
            d1 = date.fromisoformat(date_to_iso[:10])
            end = datetime.combine(
                d1 + timedelta(days=1), datetime.min.time(), tzinfo=zone
            )
            end_ms = int(end.timestamp() * 1000)
        except ValueError:
            pass
    return start_ms, end_ms


def _row_briefing_date(row: dict, tz: ZoneInfo) -> date | None:
    """Prefer briefing_date column; fall back to created_at local date."""
    raw = row.get("briefing_date")
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return date.fromisoformat(raw.strip()[:10])
        except ValueError:
            pass
    created = parse_created_at(row.get("created_at"))
    if created is None:
        return None
    return local_date_for(created, tz)


def build_briefing_history(
    rows: list[dict],
    *,
    dates: list[date] | None = None,
    tz: ZoneInfo | None = None,
) -> list[dict]:
    """
    Map DB rows into one slot per calendar day (latest created_at wins).

    Prefers `briefing_date`; falls back to local date of `created_at`.
    Pure function — no OpenAI / network calls.
    """
    zone = tz or get_briefing_tz()
    day_list = dates or history_date_range()
    latest_by_date: dict[date, dict] = {}

    for row in rows:
        local_day = _row_briefing_date(row, zone)
        if local_day is None or local_day not in day_list:
            continue
        created = parse_created_at(row.get("created_at"))
        existing = latest_by_date.get(local_day)
        if existing is None:
            latest_by_date[local_day] = row
            continue
        existing_created = parse_created_at(existing.get("created_at"))
        if created is not None and (
            existing_created is None or created > existing_created
        ):
            latest_by_date[local_day] = row

    days: list[dict] = []
    for day in day_list:
        briefing = latest_by_date.get(day)
        days.append(
            {
                "date": day.isoformat(),
                "has_briefing": briefing is not None,
                "briefing": briefing,
            }
        )
    return days


def get_briefing_history_for_account(gmail_account_email: str) -> dict:
    """Load today + prior 7 days of stored briefings (no OpenAI)."""
    zone = get_briefing_tz()
    tz_name = get_briefing_timezone_name()
    today = datetime.now(timezone.utc).astimezone(zone).date()
    dates = history_date_range(today)
    window_start = datetime.combine(dates[0], datetime.min.time(), tzinfo=zone)
    since_utc = window_start.astimezone(timezone.utc).isoformat()

    rows = list_briefings_since(
        gmail_account_email=gmail_account_email,
        since_iso=since_utc,
        since_briefing_date=dates[0].isoformat(),
    )
    days = build_briefing_history(rows, dates=dates, tz=zone)
    return {
        "timezone": tz_name,
        "today": today.isoformat(),
        "days": days,
    }


def parse_target_briefing_date(value: str | date | None) -> date:
    """Resolve target date; default today in briefing TZ. Raises ValueError."""
    zone = get_briefing_tz()
    today = datetime.now(timezone.utc).astimezone(zone).date()
    if value is None or value == "":
        return today
    if isinstance(value, date) and not isinstance(value, datetime):
        target = value
    else:
        target = date.fromisoformat(str(value).strip()[:10])
    allowed = set(history_date_range(today))
    if target not in allowed:
        raise ValueError(
            f"date must be within today and the previous {HISTORY_PRIOR_DAYS} days "
            f"({min(allowed).isoformat()} … {max(allowed).isoformat()})"
        )
    return target



def _resolve_briefing_account() -> str | None:
    """Account used to load stored emails and save briefings.

    Prefer the live Gmail profile. If the token cannot refresh, use the
    allowlisted mailbox so briefing still runs from Supabase (same fallback
    as inbox/chat).
    """
    try:
        from gmail_sync import get_profile_email
        from main import load_gmail_credentials

        return get_profile_email(load_gmail_credentials())
    except Exception as exc:
        from gmail_safety import get_allowed_gmail_account

        allowed = get_allowed_gmail_account()
        logger.warning(
            "Live Gmail unavailable for briefing (%s); using stored mailbox %s",
            type(exc).__name__,
            allowed,
        )
        return allowed or None


def _analysis_from_email(email: dict) -> dict:
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return analyses[0] if analyses else {}
    return analyses if isinstance(analyses, dict) else {}


def _build_context(emails: list[dict], zone: ZoneInfo | None = None) -> str:
    parts: list[str] = []
    for index, email in enumerate(emails, start=1):
        analysis = _analysis_from_email(email)
        deadlines = analysis.get("deadlines") or []
        body = str(email.get("body") or "")
        excerpt = body[:700] if looks_eventish(email) else ""
        parts.append(
            f"""
Email {index}
ID: {email.get("id") or ""}
Subject: {email.get("subject") or ""}
Sender: {email.get("sender") or ""}
Received: {received_iso(email, zone or get_briefing_tz())}
Importance: {analysis.get("importance") or "unknown"}
Category: {analysis.get("category") or "unknown"}
Action required: {analysis.get("action_required")}
Summary: {analysis.get("summary") or ""}
Stored deadlines: {deadlines}
Snippet: {(email.get("snippet") or "")[:400]}
Body excerpt: {excerpt}
"""
        )
    return "\n---\n".join(parts)


def _merge_briefing_emails(
    day_emails: list[dict], lookback_emails: list[dict], limit: int
) -> list[dict]:
    ordered: list[dict] = []
    seen: set[str] = set()
    for email in day_emails:
        key = str(email.get("id") or "")
        if key:
            seen.add(key)
        ordered.append(email)
    cap = max(limit, 40)
    for email in lookback_emails:
        if len(ordered) >= cap:
            break
        key = str(email.get("id") or "")
        if key and key in seen:
            continue
        if looks_eventish(email) or _analysis_from_email(email).get("deadlines"):
            if key:
                seen.add(key)
            ordered.append(email)
    return ordered


def generate_briefing_content(
    emails: list[dict],
    *,
    briefing_day: date | None = None,
    smart: bool = False,
) -> BriefingContent:
    if not emails:
        return BriefingContent(
            headline="Quiet inbox",
            highlights=[],
            deadlines=[],
            action_items=[],
            notes="No saved emails in the briefing window.",
            items=[],
            meetings=[],
            upcoming_deadlines=[],
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing")

    day = briefing_day or datetime.now(timezone.utc).date()
    target = day.isoformat()
    ordered_emails = emails
    ranking_block = ""
    if smart:
        from briefing_priority import (
            order_emails_by_priority,
            ranking_context_for_prompt,
        )

        ordered_emails = order_emails_by_priority(emails, briefing_day=day)
        ranking_block = ranking_context_for_prompt(
            ordered_emails, briefing_day=day
        )

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=0,
    ).with_structured_output(BriefingContent)

    context = _build_context(ordered_emails)
    smart_instructions = ""
    if smart:
        smart_instructions = (
            "\nSmart ranking is enabled. Lead items[] with the ranked "
            "actionable emails from the ranking block (max 3 leaders), "
            "preserve high/medium/low priority labels from context, and "
            "write concise recommended_action text grounded in those emails "
            "and approaching deadlines. Never invent priorities.\n"
        )

    human_parts = [
        f"Create the briefing for {target} (Asia/Kolkata) from these emails:"
    ]
    if ranking_block:
        human_parts.append(ranking_block)
    human_parts.append(context)

    result = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You create a concise daily executive briefing from stored "
                    "emails. Use only the provided context. Never invent "
                    "senders, subjects, dates, meeting times, or deadlines. "
                    "Prefer high-importance and action_required items.\n"
                    f"{smart_instructions}"
                    "For each items[] entry provide: a short title, a 2-3 "
                    "sentence description grounded in the email, priority "
                    "(high/medium/low), recommended_action when the user "
                    "should do something, and source_email_index matching "
                    "Email N. Keep items to max 6.\n"
                    "Meetings: extract only meetings, interviews, webinars, "
                    "or scheduled events whose EVENT date (not received date) "
                    f"is {target}. Invitation without acceptance is tentative. "
                    "Use cancelled/rescheduled/confirmed when the email says so. "
                    "Leave start_time empty if unknown. Max 8 meetings.\n"
                    "Upcoming deadlines: only personal obligations with a "
                    "specific due date supported by the email. Skip newsletter "
                    "article dates and vague phrases. Exclude dates before "
                    f"{target} unless the email marks them overdue.\n"
                    "Also fill highlights, deadlines, and action_items with "
                    "short strings for compatibility. Do not copy whole emails."
                )
            ),
            HumanMessage(content="\n\n".join(human_parts)),
        ]
    )
    return result


def run_daily_briefing(target_date: date | str | None = None) -> dict:
    """
    Build and store a daily briefing for one calendar day.

    - Scheduler / omit date → today in BRIEFING_TIMEZONE.
    - Manual generate with date → that day (must be in history window).
    - Email context: messages with internal_date on that local day.
      If empty and target is today, fall back to lookback / newest stored
      (preserves prior first-run behavior). Past days stay quiet if empty.
    """
    lookback_hours = _env_int("BRIEFING_LOOKBACK_HOURS", 24)
    max_emails = _env_int("BRIEFING_MAX_EMAILS", 30)
    zone = get_briefing_tz()
    today = datetime.now(timezone.utc).astimezone(zone).date()

    stats = {
        "email_count": 0,
        "saved": False,
        "briefing_id": None,
        "used_fallback": False,
        "status": "started",
        "briefing_date": None,
    }

    try:
        briefing_day = parse_target_briefing_date(target_date)
    except ValueError as exc:
        logger.warning("Invalid briefing target_date: %s", exc)
        stats["status"] = "invalid_date"
        stats["error"] = str(exc)
        return stats

    stats["briefing_date"] = briefing_day.isoformat()
    day_start = datetime.combine(briefing_day, datetime.min.time(), tzinfo=zone)
    day_end = day_start + timedelta(days=1)
    period_start = day_start.astimezone(timezone.utc)
    period_end = day_end.astimezone(timezone.utc)
    start_ms = int(day_start.timestamp() * 1000)
    end_ms = int(day_end.timestamp() * 1000)

    account = _resolve_briefing_account()
    if not account:
        logger.error(
            "Daily briefing save skipped: Gmail account unavailable "
            "(status=save_failed)"
        )
        stats["status"] = "save_failed"
        stats["error"] = (
            "Gmail account unavailable. Reconnect Gmail OAuth, then retry."
        )
        return stats

    try:
        emails = list_emails_for_briefing_day(
            gmail_account_email=account,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=max_emails,
        )
    except Exception:
        logger.exception(
            "Daily briefing failed while loading day emails "
            "(status=load_failed date=%s)",
            briefing_day.isoformat(),
        )
        stats["status"] = "load_failed"
        return stats

    if not emails:
        try:
            from main import load_gmail_credentials
            from scheduler import ingest_inbox_for_local_day

            credentials = load_gmail_credentials()
            logger.info(
                "No local emails for %s; ingesting that day's INBOX from Gmail",
                briefing_day.isoformat(),
            )
            ingest_inbox_for_local_day(
                credentials,
                account,
                briefing_day,
            )
            emails = list_emails_for_briefing_day(
                gmail_account_email=account,
                start_ms=start_ms,
                end_ms=end_ms,
                limit=max_emails,
            )
        except Exception as exc:
            logger.info(
                "Skipping Gmail day ingest for briefing date=%s (%s)",
                briefing_day.isoformat(),
                type(exc).__name__,
            )

    # Today-only: preserve lookback + newest-mail fallback for first-run UX.
    if not emails and briefing_day == today:
        lookback_start = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        try:
            emails = list_emails_for_briefing(
                since_iso=lookback_start.isoformat(),
                limit=max_emails,
            )
        except Exception:
            logger.exception(
                "Daily briefing lookback load failed (status=load_failed)"
            )
            stats["status"] = "load_failed"
            return stats

        if not emails:
            logger.info(
                "Daily briefing: no emails for today/lookback; "
                "falling back to newest stored emails"
            )
            try:
                emails = (
                    list_stored_emails(
                        limit=max_emails,
                        gmail_account_email=account,
                    )
                    or []
                )
                stats["used_fallback"] = True
            except Exception:
                logger.exception(
                    "Daily briefing fallback list_stored_emails failed "
                    "(status=load_failed)"
                )
                stats["status"] = "load_failed"
                return stats

    lookback_emails: list[dict] = []
    lookback_start = datetime.combine(
        briefing_day - timedelta(days=EVENT_LOOKBACK_DAYS),
        datetime.min.time(),
        tzinfo=zone,
    )
    try:
        lookback_emails = list_emails_for_briefing_day(
            gmail_account_email=account,
            start_ms=int(lookback_start.timestamp() * 1000),
            end_ms=end_ms,
            limit=max(80, max_emails * 2),
        )
    except Exception:
        logger.exception(
            "Daily briefing event lookback load failed date=%s",
            briefing_day.isoformat(),
        )
        lookback_emails = []

    llm_emails = _merge_briefing_emails(emails, lookback_emails, max_emails)
    scan_emails = lookback_emails or emails

    prefs_payload = None
    try:
        from briefing_preferences import (
            filter_emails_for_preferences,
            get_preferences_for_account,
            personalization_enabled,
        )

        if personalization_enabled():
            prefs_payload = get_preferences_for_account(account).get("preferences")
            before = len(llm_emails)
            llm_emails = filter_emails_for_preferences(llm_emails, prefs_payload)
            scan_emails = filter_emails_for_preferences(scan_emails, prefs_payload)
            stats["personalization_enabled"] = True
            stats["emails_filtered"] = max(0, before - len(llm_emails))
        else:
            stats["personalization_enabled"] = False
    except Exception:
        logger.exception(
            "Briefing personalization filter failed; continuing with unfiltered emails"
        )
        stats["personalization_fallback"] = "filter"

    stats["email_count"] = len(emails)
    logger.info(
        "Daily briefing context ready date=%s email_count=%s used_fallback=%s",
        briefing_day.isoformat(),
        stats["email_count"],
        stats["used_fallback"],
    )

    from briefing_priority import (
        apply_smart_priorities_to_payload,
        smart_briefing_enabled,
    )

    smart_enabled = smart_briefing_enabled()
    stats["smart_briefing_enabled"] = smart_enabled
    stats["smart_fallback"] = None

    try:
        content = generate_briefing_content(
            llm_emails,
            briefing_day=briefing_day,
            smart=smart_enabled,
        )
    except Exception:
        if smart_enabled:
            logger.exception(
                "Smart briefing AI generation failed; falling back to "
                "classic generate_briefing_content"
            )
            try:
                content = generate_briefing_content(
                    llm_emails,
                    briefing_day=briefing_day,
                    smart=False,
                )
                stats["smart_fallback"] = "generation"
            except Exception:
                logger.exception(
                    "Daily briefing AI generation failed after smart fallback "
                    "(status=generation_failed email_count=%s)",
                    stats["email_count"],
                )
                stats["status"] = "generation_failed"
                return stats
        else:
            logger.exception(
                "Daily briefing AI generation failed (status=generation_failed "
                "email_count=%s)",
                stats["email_count"],
            )
            stats["status"] = "generation_failed"
            return stats

    if stats["used_fallback"] and emails:
        fallback_note = (
            f"No emails were saved in the last {lookback_hours}h; "
            "this briefing uses your newest stored emails instead."
        )
        content.notes = (
            f"{fallback_note} {content.notes}".strip()
            if content.notes
            else fallback_note
        )
    elif not emails:
        content.notes = (
            content.notes
            or f"No saved emails for {briefing_day.isoformat()}."
        )
        if not content.headline:
            content.headline = f"Quiet day — {briefing_day.isoformat()}"

    extra_meetings = [
        parsed
        for email in scan_emails
        if (parsed := parse_calendar_invite(email))
    ]
    extra_deadlines: list[dict] = []
    for email in scan_emails:
        extra_deadlines.extend(extract_analysis_deadlines(email, briefing_day))

    payload = hydrate_briefing_payload(
        content,
        llm_emails,
        briefing_day=briefing_day,
        extra_meetings=extra_meetings,
        extra_deadlines=extra_deadlines,
    )
    payload["email_count"] = len(emails)
    payload["briefing_date"] = briefing_day.isoformat()

    if smart_enabled:
        try:
            payload = apply_smart_priorities_to_payload(
                payload,
                llm_emails,
                briefing_day=briefing_day,
                account_email=account,
            )
        except Exception:
            logger.exception(
                "Smart priority ranking failed; keeping classic hydrated "
                "briefing payload (single save, no duplicate row)"
            )
            if not stats.get("smart_fallback"):
                stats["smart_fallback"] = "ranking"

    from briefing_followup import (
        apply_pending_replies_to_payload,
        followup_detection_active,
        pending_reply_detection_enabled,
    )

    followup_enabled = followup_detection_active()
    stats["pending_reply_detection_enabled"] = pending_reply_detection_enabled()
    stats["followup_detection_active"] = followup_enabled
    try:
        from ai_followup_assistant import ai_followup_assistant_enabled

        stats["ai_followup_assistant_enabled"] = ai_followup_assistant_enabled()
    except Exception:
        stats["ai_followup_assistant_enabled"] = False
    stats["pending_reply_fallback"] = None
    if followup_enabled:
        try:
            payload = apply_pending_replies_to_payload(
                payload,
                llm_emails,
                account_email=account,
                gmail_account_email=account,
            )
        except Exception:
            logger.exception(
                "Pending-reply detection failed; keeping prior briefing "
                "payload (single save, no duplicate row)"
            )
            stats["pending_reply_fallback"] = "detection"

    try:
        from briefing_preferences import apply_section_preferences, personalization_enabled

        if personalization_enabled() and prefs_payload:
            payload = apply_section_preferences(payload, prefs_payload)
    except Exception:
        logger.exception(
            "Section preference apply failed; keeping prior briefing payload"
        )
        stats["personalization_fallback"] = "sections"

    try:
        from briefing_feedback import (
            filter_validated_recommendations,
            feedback_enabled,
            suppressed_recommendation_keys,
        )
        from briefing_preferences import personalization_enabled as _pers_on
        from briefing_recommendations import _stable_key

        if feedback_enabled() or _pers_on():
            known_ids = {
                str(e.get("id"))
                for e in llm_emails
                if isinstance(e, dict) and e.get("id")
            }
            suppressed = suppressed_recommendation_keys(account)
            candidates = []
            for item in payload.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = _stable_key(
                    account,
                    "briefing_item",
                    str(item.get("source_email_id") or ""),
                    str(item.get("title") or ""),
                    str(item.get("kind") or "action"),
                )
                if key in suppressed:
                    continue
                row_item = dict(item)
                row_item["recommendation_key"] = key
                row_item["source_type"] = "briefing_item"
                candidates.append(row_item)
            kept, val_stats = filter_validated_recommendations(
                candidates,
                account_email=account,
                known_email_ids=known_ids,
                seen_keys=set(),
            )
            payload = dict(payload)
            payload["items"] = kept
            stats["recommendation_validation"] = val_stats
    except Exception:
        logger.exception(
            "Recommendation validation failed; keeping prior items (no duplicate save)"
        )
        stats["feedback_fallback"] = "validation"

    try:
        row = save_briefing(
            content=payload,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            email_count=len(emails),
            gmail_account_email=account,
            briefing_date=briefing_day.isoformat(),
        )
        stats["saved"] = True
        stats["briefing_id"] = row.get("id")
        stats["status"] = "saved"
        stats["account"] = account
        logger.info(
            "Daily briefing saved id=%s date=%s account=%s email_count=%s "
            "used_fallback=%s",
            stats["briefing_id"],
            briefing_day.isoformat(),
            account,
            stats["email_count"],
            stats["used_fallback"],
        )
        try:
            from whatsapp_notifications import (
                notify_whatsapp_daily_briefing,
                notify_whatsapp_meeting_and_payment_from_briefing,
            )

            wa_stats = notify_whatsapp_daily_briefing(stats, payload)
            stats["whatsapp"] = wa_stats
            meeting_wa = notify_whatsapp_meeting_and_payment_from_briefing(
                payload,
                briefing_day=briefing_day,
            )
            stats["whatsapp_meetings_payments"] = meeting_wa
        except Exception:
            logger.exception(
                "WhatsApp notify after briefing save failed "
                "(briefing remains saved)"
            )
    except Exception as exc:
        logger.exception(
            "Daily briefing failed while saving to database "
            "(status=save_failed)"
        )
        stats["status"] = "save_failed"
        message = str(exc)
        # Prefer PostgREST structured message when present.
        detail = getattr(exc, "message", None) or message
        if isinstance(getattr(exc, "args", None), tuple) and exc.args:
            first = exc.args[0]
            if isinstance(first, dict) and first.get("message"):
                detail = str(first["message"])
            elif isinstance(first, str):
                detail = first
        if "briefing_date" in detail.lower() or "PGRST204" in detail:
            detail = (
                f"{detail} — Run docs/migrations/2026-10-19_briefing_date.sql "
                "in the Supabase SQL Editor, then reload the API schema."
            )
        stats["error"] = detail[:500]

    return stats

