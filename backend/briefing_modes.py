"""Smart Briefing Modes 2.0 — evening / weekly / monday (additive).

Daily/morning briefing stays in briefing_service.run_daily_briefing.
Mode runs are stored in smart_briefing_mode_runs (separate table).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from briefing_service import (
    _build_context,
    _resolve_briefing_account,
    get_briefing_tz,
    get_briefing_timezone_name,
)
from briefing_structure import (
    BriefingContent,
    extract_analysis_deadlines,
    hydrate_briefing_payload,
    parse_calendar_invite,
)
from db import (
    list_emails_for_briefing_range,
    list_smart_briefing_mode_runs,
    get_latest_smart_briefing_mode_run,
    upsert_smart_briefing_mode_run,
)

logger = logging.getLogger(__name__)

SUPPORTED_MODES = frozenset({"evening", "weekly", "monday"})
MODE_LABELS = {
    "evening": "Evening Wrap-up",
    "weekly": "Weekly Digest",
    "monday": "Monday Planning",
}


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def smart_briefing_modes_master_enabled() -> bool:
    return _flag("SMART_BRIEFING_MODES_ENABLED", "false")


def mode_enabled(mode: str) -> bool:
    if mode not in SUPPORTED_MODES:
        return False
    if not smart_briefing_modes_master_enabled():
        return False
    env_key = {
        "evening": "EVENING_BRIEFING_ENABLED",
        "weekly": "WEEKLY_DIGEST_ENABLED",
        "monday": "MONDAY_PLANNING_ENABLED",
    }[mode]
    return _flag(env_key, "false")


def list_mode_catalog() -> list[dict[str, Any]]:
    return [
        {
            "mode": mode,
            "label": MODE_LABELS[mode],
            "enabled": mode_enabled(mode),
            "master_enabled": smart_briefing_modes_master_enabled(),
        }
        for mode in ("evening", "weekly", "monday")
    ]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def compute_mode_period(
    mode: str,
    *,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> dict[str, Any]:
    """
    Timezone-aware reporting windows.

    evening: local calendar day [00:00, now]
    weekly: previous 7 completed local days [today-7, today) 
    monday: upcoming Mon–Sun week (this Monday if today is Monday, else next Monday)
    """
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported briefing mode: {mode}")

    zone = tz or get_briefing_tz()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    today = local_now.date()

    if mode == "evening":
        start_local = datetime.combine(today, datetime.min.time(), tzinfo=zone)
        end_local = local_now
        period_key = today.isoformat()
        label = f"Evening wrap-up {today.isoformat()}"
    elif mode == "weekly":
        # Seven completed local days ending yesterday.
        end_day = today  # exclusive end at today 00:00
        start_day = today - timedelta(days=7)
        start_local = datetime.combine(start_day, datetime.min.time(), tzinfo=zone)
        end_local = datetime.combine(end_day, datetime.min.time(), tzinfo=zone)
        period_key = f"{start_day.isoformat()}_{end_day.isoformat()}"
        label = f"Weekly digest {start_day.isoformat()} to {(end_day - timedelta(days=1)).isoformat()}"
    else:  # monday
        if today.weekday() == 0:
            week_start = today
        else:
            week_start = today + timedelta(days=(7 - today.weekday()))
        week_end = week_start + timedelta(days=7)
        start_local = datetime.combine(week_start, datetime.min.time(), tzinfo=zone)
        end_local = datetime.combine(week_end, datetime.min.time(), tzinfo=zone)
        period_key = week_start.isoformat()
        label = f"Monday planning week of {week_start.isoformat()}"

    return {
        "mode": mode,
        "period_key": period_key,
        "label": label,
        "local_start": start_local,
        "local_end": end_local,
        "period_start_utc": start_local.astimezone(timezone.utc),
        "period_end_utc": end_local.astimezone(timezone.utc),
        "start_ms": int(start_local.timestamp() * 1000),
        "end_ms": int(end_local.timestamp() * 1000),
        "timezone": get_briefing_timezone_name(),
        "anchor_date": today,
    }


def _mode_system_prompt(mode: str, period: dict[str, Any]) -> str:
    start = period["local_start"].date().isoformat()
    end = period["local_end"].date().isoformat()
    common = (
        "Use only the provided email context. Never invent senders, subjects, "
        "dates, meetings, deadlines, or accomplishments. Do not claim a task "
        "was completed merely because an email was sent. Meetings must come "
        "from email-derived invitations only (no calendar API). "
        "Preserve BriefingContent fields: headline, highlights, deadlines, "
        "action_items, notes, items, meetings, upcoming_deadlines. "
        "Each items[] entry needs title, description, priority "
        "(high/medium/low), recommended_action when useful, and "
        "source_email_index matching Email N. Max 6 items."
    )
    if mode == "evening":
        return (
            f"You write an Evening Wrap-up for {start} (local). "
            "Summarize what arrived today, important developments, outstanding "
            "actions, follow-ups, and suggested priorities for tomorrow. "
            f"{common}"
        )
    if mode == "weekly":
        return (
            f"You write a Weekly Digest covering {start} through the day before {end}. "
            "Summarize important conversations, high-priority and action-required "
            "mail, pending replies, deadlines, and suggested focus for next week. "
            "Avoid repeating the same conversation in multiple highlights. "
            f"{common}"
        )
    return (
        f"You write Monday Planning for the week starting {start}. "
        "Organize upcoming priorities, top three recommended actions, important "
        "deadlines in that week, and follow-ups. Use historical email context "
        "for outstanding requests; only place meetings/deadlines when dates "
        f"fall in the planning week when known. {common}"
    )


def generate_mode_briefing_content(
    emails: list[dict],
    *,
    mode: str,
    period: dict[str, Any],
) -> BriefingContent:
    if not emails:
        return BriefingContent(
            headline=f"{MODE_LABELS.get(mode, mode)} — quiet period",
            highlights=[],
            deadlines=[],
            action_items=[],
            notes="No saved emails in this reporting window.",
            items=[],
            meetings=[],
            upcoming_deadlines=[],
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing")

    llm = ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        api_key=api_key,
        temperature=0,
    ).with_structured_output(BriefingContent)

    context = _build_context(emails)
    human = (
        f"Mode: {MODE_LABELS.get(mode, mode)}\n"
        f"Period key: {period['period_key']}\n"
        f"Timezone: {period['timezone']}\n"
        f"Window: {period['local_start'].isoformat()} → {period['local_end'].isoformat()}\n\n"
        f"Emails:\n{context}"
    )
    return llm.invoke(
        [
            SystemMessage(content=_mode_system_prompt(mode, period)),
            HumanMessage(content=human),
        ]
    )


def _apply_optional_enhancements(
    payload: dict,
    emails: list[dict],
    *,
    account: str,
    briefing_day: date,
) -> dict:
    """Reuse Task 1 / Task 2 when those flags are on; soft-fail otherwise."""
    try:
        from briefing_priority import (
            apply_smart_priorities_to_payload,
            smart_briefing_enabled,
        )

        if smart_briefing_enabled():
            payload = apply_smart_priorities_to_payload(
                payload, emails, briefing_day=briefing_day
            )
    except Exception:
        logger.exception("Mode smart-priority enhance failed; continuing")

    try:
        from briefing_followup import (
            apply_pending_replies_to_payload,
            followup_detection_active,
        )

        if followup_detection_active():
            payload = apply_pending_replies_to_payload(
                payload,
                emails,
                account_email=account,
                gmail_account_email=account,
            )
    except Exception:
        logger.exception("Mode pending-reply enhance failed; continuing")
    return payload


def run_mode_briefing(
    mode: str,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Generate and upsert one smart briefing mode run.

    force=True bypasses per-mode enabled check (manual/internal testing only).
    Master flag still required unless force is True? Spec: manual generate
    when ENABLE_MANUAL_SYNC - allow force to run even if schedule flags off,
    but still require SMART_BRIEFING_MODES_ENABLED for safety unless force.
    """
    stats: dict[str, Any] = {
        "mode": mode,
        "saved": False,
        "status": "started",
        "briefing_id": None,
        "email_count": 0,
        "period_key": None,
    }
    if mode not in SUPPORTED_MODES:
        stats["status"] = "unsupported_mode"
        stats["error"] = f"Unsupported mode: {mode}"
        return stats

    if not force and not mode_enabled(mode):
        stats["status"] = "disabled"
        stats["error"] = (
            f"Mode {mode} is disabled. Enable SMART_BRIEFING_MODES_ENABLED "
            "and the mode-specific flag."
        )
        return stats

    account = _resolve_briefing_account()
    if not account:
        stats["status"] = "save_failed"
        stats["error"] = "Gmail account unavailable."
        return stats

    try:
        period = compute_mode_period(mode, now=now)
    except ValueError as exc:
        stats["status"] = "unsupported_mode"
        stats["error"] = str(exc)
        return stats

    stats["period_key"] = period["period_key"]
    stats["timezone"] = period["timezone"]
    max_emails = _env_int("BRIEFING_MAX_EMAILS", 30)
    range_limit = max(max_emails, 80 if mode == "weekly" else max_emails)

    # Monday planning needs historical context + week-ahead signals.
    if mode == "monday":
        hist_start = period["local_start"] - timedelta(days=14)
        load_start_ms = int(hist_start.timestamp() * 1000)
        load_end_ms = period["end_ms"]
        hydrate_day = period["local_start"].date()
    else:
        load_start_ms = period["start_ms"]
        load_end_ms = period["end_ms"]
        hydrate_day = period["local_start"].date()

    try:
        emails = list_emails_for_briefing_range(
            gmail_account_email=account,
            start_ms=load_start_ms,
            end_ms=load_end_ms,
            limit=range_limit,
        )
    except Exception:
        logger.exception("Mode %s email load failed", mode)
        stats["status"] = "load_failed"
        return stats

    stats["email_count"] = len(emails)

    try:
        content = generate_mode_briefing_content(
            emails, mode=mode, period=period
        )
    except Exception:
        logger.exception("Mode %s AI generation failed", mode)
        stats["status"] = "generation_failed"
        # Persist failure marker without fabricating content
        try:
            upsert_smart_briefing_mode_run(
                gmail_account_email=account,
                mode=mode,
                period_key=period["period_key"],
                period_start=period["period_start_utc"].isoformat(),
                period_end=period["period_end_utc"].isoformat(),
                content={
                    "headline": f"{MODE_LABELS[mode]} generation failed",
                    "notes": "Generation failed; no fabricated content saved.",
                    "mode": mode,
                    "period_key": period["period_key"],
                },
                email_count=len(emails),
                status="failed",
                error_message="generation_failed",
            )
        except Exception:
            logger.exception("Could not persist failed mode run marker")
        return stats

    extra_meetings = [
        parsed for email in emails if (parsed := parse_calendar_invite(email))
    ]
    extra_deadlines: list[dict] = []
    for email in emails:
        extra_deadlines.extend(extract_analysis_deadlines(email, hydrate_day))

    try:
        payload = hydrate_briefing_payload(
            content,
            emails,
            briefing_day=hydrate_day,
            extra_meetings=extra_meetings,
            extra_deadlines=extra_deadlines,
        )
        payload["email_count"] = len(emails)
        payload["briefing_mode"] = mode
        payload["period_key"] = period["period_key"]
        payload["period_label"] = period["label"]
        payload = _apply_optional_enhancements(
            payload,
            emails,
            account=account,
            briefing_day=hydrate_day,
        )
    except Exception:
        logger.exception("Mode %s hydrate/enhance failed", mode)
        stats["status"] = "generation_failed"
        return stats

    try:
        row = upsert_smart_briefing_mode_run(
            gmail_account_email=account,
            mode=mode,
            period_key=period["period_key"],
            period_start=period["period_start_utc"].isoformat(),
            period_end=period["period_end_utc"].isoformat(),
            content=payload,
            email_count=len(emails),
            status="saved" if emails else "empty",
            error_message=None,
        )
        stats["saved"] = True
        stats["briefing_id"] = row.get("id")
        stats["status"] = "saved" if emails else "empty"
        stats["account"] = account
    except Exception as exc:
        logger.exception("Mode %s save failed", mode)
        stats["status"] = "save_failed"
        stats["error"] = str(exc)[:200]
        if "smart_briefing_mode_runs" in str(exc) or "PGRST" in str(exc):
            stats["error"] = (
                "smart_briefing_mode_runs table missing; run "
                "docs/migrations/2026-11-03_smart_briefing_mode_runs.sql"
            )
    return stats


def get_mode_latest(mode: str, account: str) -> dict | None:
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    return get_latest_smart_briefing_mode_run(
        gmail_account_email=account, mode=mode
    )


def get_mode_history(mode: str, account: str, *, limit: int = 14) -> dict[str, Any]:
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    rows = list_smart_briefing_mode_runs(
        gmail_account_email=account, mode=mode, limit=limit
    )
    return {
        "mode": mode,
        "label": MODE_LABELS[mode],
        "timezone": get_briefing_timezone_name(),
        "runs": rows,
    }
