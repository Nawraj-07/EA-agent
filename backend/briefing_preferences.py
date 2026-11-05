"""Task 5 — Personalized briefing preferences & schedule config.

Feature flag: BRIEFING_PERSONALIZATION_ENABLED (default false).
Defaults preserve classic Daily Briefing behavior.
Does not change Daily Briefing UI layout.
"""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from briefing_structure import _analysis

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT_SECTIONS = {
    "important_emails": True,
    "pending_replies": True,
    "follow_up_recommendations": True,
    "deadlines": True,
    "meetings": True,
    "top_three_priorities": True,
    "informational_updates": True,
    "low_priority_emails": True,
}

DEFAULT_FILTERING = {
    "hide_promotional": False,
    "hide_newsletters": False,
    "hide_automated": False,
    "hide_low_priority_informational": False,
}

DEFAULT_MODE_SCHEDULE = {
    "morning": {
        "enabled": False,
        "local_time": "08:00",
        "timezone": None,
        "weekday": None,
    },
    "evening": {
        "enabled": False,
        "local_time": "17:00",
        "timezone": None,
        "weekday": None,
    },
    "weekly": {
        "enabled": False,
        "local_time": "17:00",
        "timezone": None,
        "weekday": "sat",
    },
    "monday": {
        "enabled": False,
        "local_time": "08:30",
        "timezone": None,
        "weekday": "mon",
    },
}

WEEKDAY_ALIASES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def personalization_enabled() -> bool:
    return os.getenv("BRIEFING_PERSONALIZATION_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def default_preferences(*, timezone_name: str | None = None) -> dict[str, Any]:
    from briefing_service import get_briefing_timezone_name

    tz = (timezone_name or get_briefing_timezone_name() or "Asia/Kolkata").strip()
    return {
        "timezone": tz,
        "working_days": [1, 2, 3, 4, 5],  # Mon–Fri (ISO: Mon=1 … Sun=7)
        "working_hours": {"start": "09:00", "end": "18:00"},
        "briefing_sections": deepcopy(DEFAULT_SECTIONS),
        "vip_contacts": [],
        "filtering_preferences": deepcopy(DEFAULT_FILTERING),
        "mode_schedules": deepcopy(DEFAULT_MODE_SCHEDULE),
    }


def normalize_email_address(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().lower()
    match = re.search(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", text)
    if not match:
        return None
    addr = match.group(1).lower()
    if not EMAIL_RE.match(addr):
        return None
    return addr


def extract_sender_email(email_row: dict | None) -> str | None:
    row = email_row if isinstance(email_row, dict) else {}
    for key in ("from_email", "sender_email", "from", "sender"):
        addr = normalize_email_address(str(row.get(key) or ""))
        if addr:
            return addr
    return None


def validate_timezone(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unsupported timezone: {text}") from exc
    return text


def _parse_hhmm(value: str, *, field: str) -> time:
    text = (value or "").strip()
    if not TIME_RE.match(text):
        raise ValueError(f"{field} must be HH:MM 24-hour")
    hour, minute = text.split(":")
    return time(hour=int(hour), minute=int(minute))


def validate_working_days(days: Any) -> list[int]:
    if not isinstance(days, list) or not days:
        raise ValueError("working_days must be a non-empty list of 1–7 (Mon=1)")
    out: list[int] = []
    for day in days:
        try:
            value = int(day)
        except (TypeError, ValueError) as exc:
            raise ValueError("working_days entries must be integers 1–7") from exc
        if value < 1 or value > 7:
            raise ValueError("working_days entries must be 1–7 (ISO weekday)")
        if value not in out:
            out.append(value)
    return sorted(out)


def validate_vip_contacts(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("vip_contacts must be a list of email addresses")
    out: list[str] = []
    for item in raw:
        addr = normalize_email_address(str(item or ""))
        if not addr:
            raise ValueError(f"Invalid VIP email address: {item!r}")
        if addr not in out:
            out.append(addr)
    if len(out) > 100:
        raise ValueError("vip_contacts limited to 100 addresses")
    return out


def _merge_sections(raw: Any) -> dict[str, bool]:
    base = deepcopy(DEFAULT_SECTIONS)
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError("briefing_sections must be an object")
    for key, default in DEFAULT_SECTIONS.items():
        if key in raw:
            base[key] = bool(raw[key])
        else:
            base[key] = default
    return base


def _merge_filtering(raw: Any) -> dict[str, bool]:
    base = deepcopy(DEFAULT_FILTERING)
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError("filtering_preferences must be an object")
    for key in DEFAULT_FILTERING:
        if key in raw:
            base[key] = bool(raw[key])
    return base


def _merge_mode_schedules(raw: Any, *, default_tz: str) -> dict[str, Any]:
    base = deepcopy(DEFAULT_MODE_SCHEDULE)
    for mode in base:
        base[mode]["timezone"] = default_tz
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError("mode_schedules must be an object")
    for mode, defaults in DEFAULT_MODE_SCHEDULE.items():
        incoming = raw.get(mode)
        if incoming is None:
            continue
        if not isinstance(incoming, dict):
            raise ValueError(f"mode_schedules.{mode} must be an object")
        enabled = bool(incoming.get("enabled", defaults["enabled"]))
        local_time = str(incoming.get("local_time") or defaults["local_time"]).strip()
        _parse_hhmm(local_time, field=f"mode_schedules.{mode}.local_time")
        tz_raw = incoming.get("timezone") or default_tz
        tz = validate_timezone(str(tz_raw))
        weekday = incoming.get("weekday", defaults.get("weekday"))
        if mode in {"weekly", "monday"}:
            if weekday is None:
                weekday = defaults["weekday"]
            key = str(weekday).strip().lower()
            if key not in WEEKDAY_ALIASES and key not in {str(i) for i in range(7)}:
                raise ValueError(f"Invalid weekday for {mode}: {weekday!r}")
            if key.isdigit():
                weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][int(key)]
            else:
                weekday = key[:3] if len(key) > 3 and key in WEEKDAY_ALIASES else key
                if weekday not in WEEKDAY_ALIASES and key in WEEKDAY_ALIASES:
                    weekday = key if len(key) == 3 else {
                        "monday": "mon",
                        "tuesday": "tue",
                        "wednesday": "wed",
                        "thursday": "thu",
                        "friday": "fri",
                        "saturday": "sat",
                        "sunday": "sun",
                    }.get(key, key)
        else:
            weekday = None
        base[mode] = {
            "enabled": enabled,
            "local_time": local_time,
            "timezone": tz,
            "weekday": weekday,
        }
    return base


def normalize_preferences_payload(
    payload: dict | None,
    *,
    existing: dict | None = None,
) -> dict[str, Any]:
    """Validate and normalize a PATCH body against defaults/existing."""
    base = default_preferences()
    if isinstance(existing, dict):
        for key in (
            "timezone",
            "working_days",
            "working_hours",
            "briefing_sections",
            "vip_contacts",
            "filtering_preferences",
            "mode_schedules",
        ):
            if key in existing and existing[key] is not None:
                base[key] = deepcopy(existing[key])

    data = payload if isinstance(payload, dict) else {}
    if "timezone" in data:
        base["timezone"] = validate_timezone(str(data["timezone"]))
    else:
        base["timezone"] = validate_timezone(str(base["timezone"]))

    if "working_days" in data:
        base["working_days"] = validate_working_days(data["working_days"])
    else:
        base["working_days"] = validate_working_days(base["working_days"])

    hours = data.get("working_hours", base.get("working_hours") or {})
    if not isinstance(hours, dict):
        raise ValueError("working_hours must be an object with start/end")
    start = str(hours.get("start") or "09:00")
    end = str(hours.get("end") or "18:00")
    start_t = _parse_hhmm(start, field="working_hours.start")
    end_t = _parse_hhmm(end, field="working_hours.end")
    if end_t <= start_t:
        raise ValueError("working_hours.end must be after start")
    base["working_hours"] = {"start": start, "end": end}

    if "briefing_sections" in data or not base.get("briefing_sections"):
        base["briefing_sections"] = _merge_sections(
            data.get("briefing_sections", base.get("briefing_sections"))
        )
    else:
        base["briefing_sections"] = _merge_sections(base.get("briefing_sections"))

    if "vip_contacts" in data:
        base["vip_contacts"] = validate_vip_contacts(data["vip_contacts"])
    else:
        base["vip_contacts"] = validate_vip_contacts(base.get("vip_contacts") or [])

    base["filtering_preferences"] = _merge_filtering(
        data.get("filtering_preferences", base.get("filtering_preferences"))
    )
    base["mode_schedules"] = _merge_mode_schedules(
        data.get("mode_schedules", base.get("mode_schedules")),
        default_tz=base["timezone"],
    )
    return base


def get_preferences_for_account(account_email: str) -> dict[str, Any]:
    """Return stored prefs or defaults. Never invents VIP contacts."""
    defaults = default_preferences()
    if not account_email:
        return {"preferences": defaults, "source": "defaults", "id": None}
    try:
        from db import get_briefing_preferences

        row = get_briefing_preferences(gmail_account_email=account_email)
    except Exception:
        logger.exception("Failed loading briefing preferences; using defaults")
        return {"preferences": defaults, "source": "defaults", "id": None}

    if not row:
        return {"preferences": defaults, "source": "defaults", "id": None}

    try:
        prefs = normalize_preferences_payload(
            {
                "timezone": row.get("timezone"),
                "working_days": row.get("working_days"),
                "working_hours": row.get("working_hours"),
                "briefing_sections": row.get("briefing_sections"),
                "vip_contacts": row.get("vip_contacts"),
                "filtering_preferences": row.get("filtering_preferences"),
                "mode_schedules": row.get("mode_schedules"),
            }
        )
    except ValueError:
        logger.exception("Stored preferences invalid; using defaults")
        return {"preferences": defaults, "source": "defaults", "id": row.get("id")}

    return {
        "preferences": prefs,
        "source": "stored",
        "id": row.get("id"),
        "updated_at": row.get("updated_at"),
    }


def save_preferences_for_account(
    account_email: str,
    payload: dict,
) -> dict[str, Any]:
    current = get_preferences_for_account(account_email)
    existing = current.get("preferences") if current.get("source") == "stored" else None
    prefs = normalize_preferences_payload(payload, existing=existing)
    from db import upsert_briefing_preferences

    row = upsert_briefing_preferences(
        gmail_account_email=account_email,
        preferences=prefs,
    )
    return {
        "preferences": prefs,
        "source": "stored",
        "id": row.get("id"),
        "updated_at": row.get("updated_at"),
    }


def is_within_working_hours(
    prefs: dict[str, Any],
    *,
    when: datetime | None = None,
) -> bool:
    """True if `when` falls on a working day inside working hours (DST-aware)."""
    from datetime import timezone as tz_utc

    tz_name = validate_timezone(str(prefs.get("timezone") or "Asia/Kolkata"))
    zone = ZoneInfo(tz_name)
    current = when or datetime.now(tz_utc.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    local = current.astimezone(zone)
    # ISO weekday: Mon=1 … Sun=7
    if local.isoweekday() not in set(prefs.get("working_days") or [1, 2, 3, 4, 5]):
        return False
    hours = prefs.get("working_hours") or {}
    start = _parse_hhmm(str(hours.get("start") or "09:00"), field="start")
    end = _parse_hhmm(str(hours.get("end") or "18:00"), field="end")
    now_t = local.time().replace(second=0, microsecond=0)
    return start <= now_t < end


def vip_contact_set(prefs: dict[str, Any] | None) -> set[str]:
    contacts = (prefs or {}).get("vip_contacts") or []
    out: set[str] = set()
    for item in contacts:
        addr = normalize_email_address(str(item))
        if addr:
            out.add(addr)
    return out


def sender_is_vip(email_row: dict | None, vip_emails: set[str]) -> bool:
    if not vip_emails:
        return False
    addr = extract_sender_email(email_row)
    return bool(addr and addr in vip_emails)


def _label_ids(email: dict) -> set[str]:
    raw = email.get("label_ids") or []
    if isinstance(raw, list):
        return {str(x) for x in raw}
    return set()


def _looks_newsletter(email: dict, analysis: dict) -> bool:
    sender = str(email.get("sender") or email.get("from_email") or "").lower()
    subject = str(email.get("subject") or "").lower()
    if "newsletter" in sender or "newsletter" in subject:
        return True
    summary = str(analysis.get("summary") or "").lower()
    return "newsletter" in summary


def _looks_automated(email: dict, analysis: dict) -> bool:
    sender = str(email.get("sender") or email.get("from_email") or "").lower()
    if any(
        token in sender
        for token in ("noreply@", "no-reply@", "donotreply@", "notifications@")
    ):
        return True
    return bool(analysis.get("is_automated"))


def email_is_urgent_override(email: dict) -> bool:
    """Important / actionable mail must not be filtered out."""
    analysis = _analysis(email)
    importance = str(analysis.get("importance") or "").lower()
    if importance == "high":
        return True
    if analysis.get("action_required") in (True, "true", "True", 1, "1"):
        return True
    deadlines = analysis.get("deadlines")
    if isinstance(deadlines, list) and deadlines:
        return True
    if isinstance(deadlines, str) and deadlines.strip():
        return True
    return False


def should_filter_email(email: dict, prefs: dict[str, Any]) -> bool:
    """
    Return True if the email should be excluded from briefing context.
    Never filters urgent/high/actionable overrides. Never deletes store rows.
    """
    filtering = prefs.get("filtering_preferences") or DEFAULT_FILTERING
    if not any(filtering.get(k) for k in DEFAULT_FILTERING):
        return False
    if email_is_urgent_override(email):
        return False
    if sender_is_vip(email, vip_contact_set(prefs)):
        return False

    analysis = _analysis(email)
    labels = _label_ids(email)
    importance = str(analysis.get("importance") or "").lower()

    if filtering.get("hide_promotional") and (
        "CATEGORY_PROMOTIONS" in labels or "PROMOTIONS" in labels
    ):
        return True
    if filtering.get("hide_newsletters") and _looks_newsletter(email, analysis):
        return True
    if filtering.get("hide_automated") and _looks_automated(email, analysis):
        return True
    if filtering.get("hide_low_priority_informational"):
        if importance == "low" and not analysis.get("action_required"):
            return True
    return False


def filter_emails_for_preferences(
    emails: list[dict] | None,
    prefs: dict[str, Any] | None,
) -> list[dict]:
    rows = [e for e in (emails or []) if isinstance(e, dict)]
    if not prefs or not personalization_enabled():
        return rows
    return [email for email in rows if not should_filter_email(email, prefs)]


def apply_section_preferences(
    payload: dict,
    prefs: dict[str, Any] | None,
) -> dict:
    """
    Influence which sections are populated. Required BriefingContent keys remain.
    Empty lists preserve schema; does not remove fields.
    """
    if not isinstance(payload, dict):
        return payload
    if not prefs or not personalization_enabled():
        return payload

    sections = prefs.get("briefing_sections") or DEFAULT_SECTIONS
    out = dict(payload)

    if not sections.get("meetings", True):
        out["meetings"] = []
    if not sections.get("deadlines", True):
        out["upcoming_deadlines"] = []
        out["deadlines"] = []

    items = [i for i in (out.get("items") or []) if isinstance(i, dict)]
    if not sections.get("pending_replies", True) or not sections.get(
        "follow_up_recommendations", True
    ):
        items = [
            i
            for i in items
            if str(i.get("kind") or "").lower() not in {"followup", "pending_reply"}
            and "follow" not in str(i.get("recommended_action") or "").lower()
        ]
    if not sections.get("informational_updates", True):
        items = [i for i in items if str(i.get("kind") or "") != "update"]
    if not sections.get("low_priority_emails", True):
        items = [i for i in items if str(i.get("priority") or "").lower() != "low"]
    if not sections.get("important_emails", True):
        # Keep actionable / deadline items only
        items = [
            i
            for i in items
            if str(i.get("priority") or "").lower() == "high"
            or str(i.get("kind") or "") == "action"
        ]
    if not sections.get("top_three_priorities", True):
        smart = dict(out.get("smart_briefing") or {})
        if smart:
            smart["top_priority_count"] = 0
            smart["top_priority_email_ids"] = []
            out["smart_briefing"] = smart
        # Drop leading priority-scored duplicates beyond classic items
        items = [i for i in items if i.get("priority_score") is None]

    out["items"] = items
    return out


def parse_schedule_hhmm(local_time: str) -> tuple[int, int]:
    parsed = _parse_hhmm(local_time, field="local_time")
    return parsed.hour, parsed.minute
