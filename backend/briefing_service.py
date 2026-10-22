"""Daily email briefing generation (Task 6.2) + history helpers."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

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


class BriefingContent(BaseModel):
    headline: str = Field(..., min_length=1)
    highlights: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description="Short note when inbox is quiet or context is limited",
    )


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
    """Connected Gmail address for scoping saved briefings."""
    try:
        from gmail_sync import get_profile_email
        from main import load_gmail_credentials

        return get_profile_email(load_gmail_credentials())
    except Exception as exc:
        logger.warning(
            "Could not resolve Gmail account for briefing save: %s",
            type(exc).__name__,
        )
        return None


def _analysis_from_email(email: dict) -> dict:
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return analyses[0] if analyses else {}
    return analyses if isinstance(analyses, dict) else {}


def _build_context(emails: list[dict]) -> str:
    parts: list[str] = []
    for index, email in enumerate(emails, start=1):
        analysis = _analysis_from_email(email)
        deadlines = analysis.get("deadlines") or []
        parts.append(
            f"""
Email {index}
Subject: {email.get("subject") or ""}
Sender: {email.get("sender") or ""}
Date: {email.get("received_at") or ""}
Importance: {analysis.get("importance") or "unknown"}
Category: {analysis.get("category") or "unknown"}
Action required: {analysis.get("action_required")}
Summary: {analysis.get("summary") or ""}
Deadlines: {deadlines}
Snippet: {(email.get("snippet") or "")[:400]}
"""
        )
    return "\n---\n".join(parts)


def generate_briefing_content(emails: list[dict]) -> BriefingContent:
    if not emails:
        return BriefingContent(
            headline="Quiet inbox",
            highlights=[],
            deadlines=[],
            action_items=[],
            notes="No saved emails in the briefing window.",
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=0,
    ).with_structured_output(BriefingContent)

    context = _build_context(emails)
    result = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You create a concise daily executive briefing from email "
                    "summaries. Use only the provided context. Do not invent "
                    "deadlines, senders, or actions. Prefer high-importance and "
                    "action_required items. Keep lists short (max 5 items each)."
                )
            ),
            HumanMessage(
                content=(
                    f"Create today's briefing from these emails:\n\n{context}"
                )
            ),
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

            logger.info(
                "No local emails for %s; ingesting that day's INBOX from Gmail",
                briefing_day.isoformat(),
            )
            ingest_inbox_for_local_day(
                load_gmail_credentials(),
                account,
                briefing_day,
            )
            emails = list_emails_for_briefing_day(
                gmail_account_email=account,
                start_ms=start_ms,
                end_ms=end_ms,
                limit=max_emails,
            )
        except Exception:
            logger.exception(
                "Gmail day ingest for briefing failed date=%s",
                briefing_day.isoformat(),
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

    stats["email_count"] = len(emails)
    logger.info(
        "Daily briefing context ready date=%s email_count=%s used_fallback=%s",
        briefing_day.isoformat(),
        stats["email_count"],
        stats["used_fallback"],
    )

    try:
        content = generate_briefing_content(emails)
    except Exception:
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

    payload = {
        "headline": content.headline,
        "highlights": content.highlights,
        "deadlines": content.deadlines,
        "action_items": content.action_items,
        "notes": content.notes,
        "email_count": len(emails),
        "briefing_date": briefing_day.isoformat(),
    }

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

