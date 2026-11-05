"""Interactive briefing recommendations (Task 4).

Builds actionable recommendations from stored briefing content + email refs.
Persists status/snooze in briefing_recommendation_state (separate from content JSONB).
Feature flag: INTERACTIVE_BRIEFING_ENABLED (default false).

UI note (layout preserved): DailyBriefingBody already opens source_email_id via
onOpenEmail (view_email). New snooze/complete/dismiss/draft controls are NOT
rendered yet — adding them would change card structure/spacing. Minimum UI
adjustment if approved later: reuse existing list-item click handlers and add
compact action icons inside current recommendation rows only, without new cards.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from briefing_service import get_briefing_tz, get_briefing_timezone_name

logger = logging.getLogger(__name__)

ACTION_TYPES = frozenset(
    {"view_email", "draft_reply", "snooze", "mark_complete", "dismiss"}
)
STATUSES = frozenset({"pending", "completed", "snoozed", "dismissed"})
SNOOZE_PRESETS = frozenset({"later_today", "tomorrow", "next_week"})
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


def interactive_briefing_enabled() -> bool:
    return os.getenv("INTERACTIVE_BRIEFING_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def recommendation_actions_enabled() -> bool:
    """Task 4 interactive OR AI Follow-up Assistant action endpoints."""
    if interactive_briefing_enabled():
        return True
    try:
        from ai_followup_assistant import ai_followup_assistant_enabled

        return ai_followup_assistant_enabled()
    except Exception:
        return False


def user_briefing_preferences(account_email: str | None = None) -> dict[str, Any]:
    """Safe defaults; optionally merge stored Task 5 prefs when personalization is on."""
    tone = (os.getenv("BRIEFING_PREFERRED_TONE") or "professional").strip() or "professional"
    base = {
        "tone": tone,
        "work_hours_start": 9,
        "work_hours_end": 18,
        "timezone": get_briefing_timezone_name(),
    }
    if not account_email:
        return base
    try:
        from briefing_preferences import get_preferences_for_account, personalization_enabled

        if not personalization_enabled():
            return base
        prefs = get_preferences_for_account(account_email).get("preferences") or {}
        hours = prefs.get("working_hours") or {}
        start = str(hours.get("start") or "09:00")
        end = str(hours.get("end") or "18:00")
        base["timezone"] = prefs.get("timezone") or base["timezone"]
        try:
            base["work_hours_start"] = int(start.split(":")[0])
            base["work_hours_end"] = int(end.split(":")[0])
        except (TypeError, ValueError, IndexError):
            pass
        base["vip_contacts"] = list(prefs.get("vip_contacts") or [])
    except Exception:
        logger.exception("Could not merge stored briefing preferences")
    return base


def _stable_key(*parts: str) -> str:
    raw = "|".join(str(p or "").strip() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _is_uuid(value: str | None) -> bool:
    return bool(value and UUID_RE.match(str(value).strip()))


def build_recommendations_from_briefing(
    briefing_row: dict | None,
    *,
    account_email: str,
) -> list[dict[str, Any]]:
    """
    Derive recommendations from a daily_briefings row content.

    Never invents email IDs. Actions requiring a missing source are marked unavailable.
    """
    if not isinstance(briefing_row, dict):
        return []
    content = briefing_row.get("content")
    if not isinstance(content, dict):
        return []

    prefs = user_briefing_preferences()
    out: list[dict[str, Any]] = []
    briefing_id = str(briefing_row.get("id") or "")
    briefing_date = str(
        briefing_row.get("briefing_date") or content.get("briefing_date") or ""
    )

    def add_item(
        *,
        source_type: str,
        source_email_id: str | None,
        title: str,
        reason: str,
        priority: str | None,
        gmail_thread_id: str | None = None,
        kind: str = "action",
    ) -> None:
        email_ok = _is_uuid(source_email_id)
        key = _stable_key(
            account_email,
            source_type,
            source_email_id or "",
            title,
            kind,
        )
        actions = []
        for action in ("view_email", "draft_reply", "snooze", "mark_complete", "dismiss"):
            available = True
            unavailable_reason = None
            if action in {"view_email", "draft_reply"} and not email_ok:
                available = False
                unavailable_reason = "source_email_missing"
            actions.append(
                {
                    "type": action,
                    "available": available,
                    "unavailable_reason": unavailable_reason,
                }
            )
        out.append(
            {
                "recommendation_key": key,
                "source_type": source_type,
                "source_id": source_email_id or key,
                "source_email_id": source_email_id if email_ok else None,
                "gmail_thread_id": gmail_thread_id,
                "title": (title or "Recommendation")[:200],
                "reason": (reason or "")[:500],
                "priority": (priority or "medium").lower()
                if (priority or "").lower() in {"high", "medium", "low"}
                else "medium",
                "actions": actions,
                "briefing_id": briefing_id or None,
                "briefing_date": briefing_date or None,
                "preferences_applied": {"tone": prefs["tone"]},
            }
        )

    for item in content.get("items") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("subject") or "").strip()
        if not title:
            continue
        item_kind = str(item.get("kind") or "action").strip().lower()
        source_type = (
            "followup"
            if item_kind in {"followup", "pending_reply"}
            or item.get("followup_state")
            else "briefing_item"
        )
        # Prefer stable follow-up key when Assistant attached one
        preset_key = str(item.get("recommendation_key") or "").strip()
        add_item(
            source_type=source_type,
            source_email_id=str(item.get("source_email_id") or "").strip() or None,
            title=title,
            reason=str(
                item.get("recommended_action")
                or item.get("priority_reason")
                or item.get("followup_reason")
                or item.get("description")
                or ""
            ),
            priority=str(item.get("priority") or "medium"),
            gmail_thread_id=str(item.get("gmail_thread_id") or "").strip() or None,
            kind=item_kind or "action",
        )
        if preset_key and out:
            out[-1]["recommendation_key"] = preset_key
            out[-1]["source_id"] = str(
                item.get("source_email_id") or item.get("gmail_thread_id") or preset_key
            )
            if source_type == "followup":
                out[-1]["followup_state"] = item.get("followup_state")
                out[-1]["waiting_hours"] = item.get("waiting_hours")
                out[-1]["attempt_count"] = item.get("followup_attempt_count")
                out[-1]["evidence_status"] = item.get("evidence_status") or item.get(
                    "followup_confidence"
                )

    for meeting in content.get("meetings") or []:
        if not isinstance(meeting, dict):
            continue
        if str(meeting.get("status") or "").lower() == "cancelled":
            continue
        title = str(meeting.get("event_title") or meeting.get("subject") or "").strip()
        if not title:
            continue
        add_item(
            source_type="meeting",
            source_email_id=str(meeting.get("source_email_id") or "").strip() or None,
            title=f"Meeting: {title}",
            reason="Email-derived meeting — review invite details.",
            priority="medium",
        )

    for deadline in content.get("upcoming_deadlines") or []:
        if not isinstance(deadline, dict):
            continue
        title = str(deadline.get("title") or deadline.get("subject") or "").strip()
        if not title:
            continue
        due = str(deadline.get("due_date") or "")
        add_item(
            source_type="deadline",
            source_email_id=str(deadline.get("source_email_id") or "").strip() or None,
            title=title,
            reason=f"Deadline {due}".strip()
            if due
            else str(deadline.get("recommended_action") or "Review deadline."),
            priority=str(deadline.get("priority") or "medium"),
        )

    # Deduplicate by recommendation_key
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in out:
        key = row["recommendation_key"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def compute_snooze_until(
    preset: str | None,
    *,
    custom_iso: str | None = None,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> datetime:
    zone = tz or get_briefing_tz()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(zone)

    if custom_iso:
        text = custom_iso.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(timezone.utc)

    choice = (preset or "").strip().lower()
    if choice not in SNOOZE_PRESETS:
        raise ValueError("Invalid snooze preset. Use later_today, tomorrow, next_week, or custom.")

    if choice == "later_today":
        target = local + timedelta(hours=3)
        end_of_day = datetime.combine(local.date(), datetime.max.time(), tzinfo=zone).replace(
            microsecond=0
        )
        if target > end_of_day:
            target = end_of_day
        return target.astimezone(timezone.utc)

    if choice == "tomorrow":
        tomorrow = local.date() + timedelta(days=1)
        target = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        return target.astimezone(timezone.utc)

    # next_week: next Monday 09:00 local
    days_ahead = (7 - local.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_mon = local.date() + timedelta(days=days_ahead)
    target = datetime.combine(next_mon, datetime.min.time(), tzinfo=zone).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    return target.astimezone(timezone.utc)


def _unsnooze_expired(account: str, now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    try:
        from db import release_expired_recommendation_snoozes

        release_expired_recommendation_snoozes(
            gmail_account_email=account,
            now_iso=current.astimezone(timezone.utc).isoformat(),
        )
    except Exception:
        logger.exception("Failed releasing expired recommendation snoozes")


def list_active_recommendations(
    *,
    account_email: str,
    include_inactive: bool = False,
    followups_only: bool = False,
) -> dict[str, Any]:
    if not recommendation_actions_enabled():
        return {
            "enabled": False,
            "recommendations": [],
            "message": "Interactive briefing / AI Follow-up Assistant is disabled.",
        }

    _unsnooze_expired(account_email)

    from db import get_latest_briefing, list_recommendation_states_for_account

    briefing = get_latest_briefing(gmail_account_email=account_email)
    built = build_recommendations_from_briefing(
        briefing, account_email=account_email
    )
    states = {
        str(row.get("recommendation_key")): row
        for row in list_recommendation_states_for_account(
            gmail_account_email=account_email
        )
    }

    suppressed: set[str] = set()
    try:
        from briefing_feedback import feedback_enabled, suppressed_recommendation_keys

        if feedback_enabled() and not include_inactive:
            suppressed = suppressed_recommendation_keys(account_email)
    except Exception:
        logger.exception("Failed loading feedback suppressions")

    merged: list[dict[str, Any]] = []
    for rec in built:
        if followups_only and str(rec.get("source_type") or "") != "followup":
            continue
        key = rec["recommendation_key"]
        if key in suppressed and not include_inactive:
            continue
        state = states.get(key) or {}
        status = str(state.get("status") or "pending")
        snoozed_until = state.get("snoozed_until")
        rec = dict(rec)
        rec["id"] = state.get("id")  # persistent row id when present
        rec["status"] = status
        rec["snoozed_until"] = snoozed_until
        rec["state_updated_at"] = state.get("updated_at")
        meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        if meta.get("attempt_count") is not None:
            rec["attempt_count"] = meta.get("attempt_count")
        if not include_inactive and status in {"completed", "dismissed"}:
            continue
        if not include_inactive and status == "snoozed":
            # still snoozed in future
            continue
        merged.append(rec)

    # Prefer high priority first
    rank = {"high": 0, "medium": 1, "low": 2}
    merged.sort(key=lambda row: (rank.get(row.get("priority"), 9), row.get("title") or ""))
    return {
        "enabled": True,
        "briefing_id": briefing.get("id") if briefing else None,
        "recommendations": merged,
        "preferences": user_briefing_preferences(),
    }


def get_owned_recommendation(
    *,
    account_email: str,
    recommendation_key: str,
) -> dict[str, Any] | None:
    listing = list_active_recommendations(
        account_email=account_email, include_inactive=True
    )
    for row in listing.get("recommendations") or []:
        if row.get("recommendation_key") == recommendation_key or str(
            row.get("id") or ""
        ) == str(recommendation_key):
            return row
    # Also allow lookup by key even if filtered — rebuild
    from db import get_latest_briefing, get_recommendation_state

    briefing = get_latest_briefing(gmail_account_email=account_email)
    for rec in build_recommendations_from_briefing(
        briefing, account_email=account_email
    ):
        if rec["recommendation_key"] == recommendation_key:
            state = get_recommendation_state(
                gmail_account_email=account_email,
                recommendation_key=recommendation_key,
            )
            rec = dict(rec)
            if state:
                rec["id"] = state.get("id")
                rec["status"] = state.get("status")
                rec["snoozed_until"] = state.get("snoozed_until")
            return rec
    return None


def update_recommendation_status(
    *,
    account_email: str,
    recommendation_key: str,
    status: str,
) -> dict[str, Any]:
    # snoozed is only via snooze_recommendation; reject other illegal transitions here
    allowed = {"pending", "completed", "dismissed"}
    if status not in allowed:
        raise ValueError("status must be pending, completed, or dismissed")

    rec = get_owned_recommendation(
        account_email=account_email, recommendation_key=recommendation_key
    )
    if not rec:
        raise KeyError("recommendation_not_found")

    key = rec["recommendation_key"]
    from db import upsert_recommendation_state

    action_type = {
        "completed": "mark_complete",
        "dismissed": "dismiss",
        "pending": None,
    }.get(status)

    row = upsert_recommendation_state(
        gmail_account_email=account_email,
        source_type=rec.get("source_type") or "briefing_item",
        source_id=str(rec.get("source_id") or key),
        recommendation_key=key,
        status=status,
        snoozed_until=None,
        title=rec.get("title"),
        action_type=action_type,
        metadata={"source_email_id": rec.get("source_email_id")},
    )
    return {"recommendation": {**rec, "status": row.get("status"), "id": row.get("id")}}


def snooze_recommendation(
    *,
    account_email: str,
    recommendation_key: str,
    preset: str | None = None,
    custom_iso: str | None = None,
) -> dict[str, Any]:
    rec = get_owned_recommendation(
        account_email=account_email, recommendation_key=recommendation_key
    )
    if not rec:
        raise KeyError("recommendation_not_found")

    until = compute_snooze_until(preset, custom_iso=custom_iso)
    key = rec["recommendation_key"]
    from db import upsert_recommendation_state

    row = upsert_recommendation_state(
        gmail_account_email=account_email,
        source_type=rec.get("source_type") or "briefing_item",
        source_id=str(rec.get("source_id") or key),
        recommendation_key=key,
        status="snoozed",
        snoozed_until=until.isoformat(),
        title=rec.get("title"),
        action_type="snooze",
        metadata={"source_email_id": rec.get("source_email_id"), "preset": preset},
    )
    return {
        "recommendation": {
            **rec,
            "status": "snoozed",
            "snoozed_until": row.get("snoozed_until"),
            "id": row.get("id"),
        }
    }


def draft_for_recommendation(
    *,
    account_email: str,
    recommendation_key: str,
) -> dict[str, Any]:
    """Generate a draft for review only — never sends."""
    rec = get_owned_recommendation(
        account_email=account_email, recommendation_key=recommendation_key
    )
    if not rec:
        raise KeyError("recommendation_not_found")

    email_id = rec.get("source_email_id")
    if not _is_uuid(email_id):
        raise ValueError("draft_unavailable_missing_source")

    from db import get_stored_email, save_draft
    from draft_service import generate_email_draft, is_no_reply_needed

    email = get_stored_email(str(email_id))
    if not email:
        raise KeyError("source_email_missing")
    if str(email.get("gmail_account_email") or "").lower() != account_email.lower():
        raise PermissionError("forbidden")

    # Never counts as a sent follow-up attempt — drafts are review-only.
    body = generate_email_draft(email)
    if is_no_reply_needed(body):
        return {
            "ok": False,
            "draft": None,
            "message": "This email does not need a reply.",
            "recommendation_key": rec["recommendation_key"],
        }

    saved = save_draft(str(email_id), body)
    return {
        "ok": True,
        "draft": {
            "id": saved.get("id"),
            "email_id": saved.get("email_id") or email_id,
            "body": saved.get("body") or body,
            "status": saved.get("status") or "pending",
        },
        "recommendation_key": rec["recommendation_key"],
        "note": (
            "Draft saved for review. Nothing was sent. "
            "Drafts do not count as follow-up attempts."
        ),
        "attempt_counted": False,
    }


def recommendations_chat_context(account_email: str) -> str:
    """Compact context block for chat_service when interactive briefing is on."""
    if not interactive_briefing_enabled():
        return ""
    try:
        payload = list_active_recommendations(account_email=account_email)
    except Exception:
        logger.exception("Failed building recommendation chat context")
        return ""
    rows = payload.get("recommendations") or []
    if not rows:
        return "Active briefing recommendations: none pending."
    lines = ["Active briefing recommendations (authorized mailbox only):"]
    for index, row in enumerate(rows[:8], start=1):
        lines.append(
            f"{index}. key={row.get('recommendation_key')} "
            f"priority={row.get('priority')} "
            f"title={row.get('title')!r} "
            f"email_id={row.get('source_email_id')} "
            f"reason={row.get('reason')!r}"
        )
    lines.append(
        "Resolve phrases like 'first priority' or 'highest-priority email' "
        "against this list. Never invent emails."
    )
    return "\n".join(lines)
