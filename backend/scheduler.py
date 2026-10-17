
"""
Scheduled jobs (Tasks 6.1–6.2).

- Gmail inbox sync: fetch recent INBOX messages, skip ones already in
  Supabase, then analyze + save new ones.
- Daily briefing: summarize recent stored emails into daily_briefings.

LIMITATIONS / PRODUCTION NOTES
------------------------------
- Designed for a single scheduler process. Do not run ENABLE_SCHEDULER=true
  on multiple Uvicorn workers or multiple deployed replicas without a
  distributed lock or a dedicated worker process — otherwise the same
  inbox may be synced concurrently.
- Prefer: one API process with ENABLE_SCHEDULER=true, or a separate
  process that only runs the scheduler (`python scheduler.py`).
- Each sync run covers a bounded window of the most recent INBOX messages
  (default 50). Older unread mail outside that window is not scanned.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ai_service import analyze_email_content
from db import email_exists_by_gmail_id, save_email_and_analysis


logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_logging_configured = False

DEFAULT_SYNC_INTERVAL_MINUTES = 15
DEFAULT_MAX_MESSAGES = 50
DEFAULT_BRIEFING_HOUR_UTC = 8
DEFAULT_BRIEFING_MINUTE_UTC = 0


def _configure_scheduler_logging() -> None:
    """
    Uvicorn's default logging config does not attach handlers to the root
    logger, so INFO from the `scheduler` logger is normally invisible.
    Mirror Uvicorn's stderr handler so startup/shutdown lines appear.
    """
    global _logging_configured
    if _logging_configured:
        return

    target = logging.getLogger("scheduler")
    target.setLevel(logging.INFO)

    if not target.handlers:
        uvicorn_error = logging.getLogger("uvicorn.error")
        if uvicorn_error.handlers:
            for handler in uvicorn_error.handlers:
                target.addHandler(handler)
            target.propagate = False
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(levelname)s:     %(name)s: %(message)s")
            )
            target.addHandler(handler)
            target.propagate = False

    _logging_configured = True


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def scheduler_enabled() -> bool:
    """Scheduler is off by default and skipped during pytest."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if _env_flag("DISABLE_SCHEDULER_FOR_TESTS", "false"):
        return False
    return _env_flag("ENABLE_SCHEDULER", "false")


def _sync_interval_minutes() -> int:
    raw = os.getenv("GMAIL_SYNC_INTERVAL_MINUTES", str(DEFAULT_SYNC_INTERVAL_MINUTES))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid GMAIL_SYNC_INTERVAL_MINUTES=%r; using %s",
            raw,
            DEFAULT_SYNC_INTERVAL_MINUTES,
        )
        return DEFAULT_SYNC_INTERVAL_MINUTES
    return max(1, value)


def _max_messages_per_run() -> int:
    raw = os.getenv("GMAIL_SYNC_MAX_MESSAGES", str(DEFAULT_MAX_MESSAGES))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_MESSAGES
    return max(1, min(value, 200))


def _list_recent_inbox_message_ids(credentials: Any, max_messages: int) -> list[str]:
    """
    Fetch recent INBOX message IDs (newest first), with pagination.

    Bounded window: at most `max_messages` IDs per run (default 50).
    """
    service = build("gmail", "v1", credentials=credentials)
    message_ids: list[str] = []
    page_token: str | None = None

    while len(message_ids) < max_messages:
        page_size = min(50, max_messages - len(message_ids))
        result = (
            service.users()
            .messages()
            .list(
                userId="me",
                labelIds=["INBOX"],
                maxResults=page_size,
                pageToken=page_token,
            )
            .execute()
        )

        for item in result.get("messages") or []:
            msg_id = item.get("id")
            if msg_id:
                message_ids.append(msg_id)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return message_ids


def sync_gmail_inbox() -> dict[str, int]:
    """
    Sync recent Gmail INBOX messages into Supabase via the existing AI pipeline.

    Safe when Gmail auth is missing/expired: logs and returns without raising.
    Does not send mail, generate drafts, or approve anything.
    """
    # Late import avoids circular import with main.py lifespan wiring.
    from fastapi import HTTPException
    from main import fetch_email_by_id, load_gmail_credentials

    stats = {"checked": 0, "skipped": 0, "saved": 0, "failed": 0}

    try:
        credentials = load_gmail_credentials()
    except HTTPException as exc:
        logger.warning(
            "Gmail sync skipped: authentication unavailable (%s)",
            getattr(exc, "detail", type(exc).__name__),
        )
        return stats
    except Exception:
        logger.exception("Gmail sync skipped: unexpected auth failure")
        return stats

    max_messages = _max_messages_per_run()

    try:
        message_ids = _list_recent_inbox_message_ids(credentials, max_messages)
    except HttpError as exc:
        logger.warning(
            "Gmail sync failed while listing messages (status=%s)",
            getattr(exc.resp, "status", "?"),
        )
        return stats
    except Exception:
        logger.exception("Gmail sync failed while listing messages")
        return stats

    logger.info(
        "Gmail sync started: checking up to %s recent INBOX messages",
        len(message_ids),
    )

    for message_id in message_ids:
        stats["checked"] += 1

        try:
            if email_exists_by_gmail_id(message_id):
                stats["skipped"] += 1
                continue

            email = fetch_email_by_id(message_id)

            analysis = analyze_email_content(
                subject=email.get("subject") or "",
                sender=email.get("from") or "",
                body=email.get("body") or "",
            )
            save_email_and_analysis(email, analysis.model_dump())
            stats["saved"] += 1
            logger.info("Gmail sync saved message_id=%s", message_id)

        except Exception as exc:
            stats["failed"] += 1
            logger.error(
                "Gmail sync failed for message_id=%s error=%s",
                message_id,
                type(exc).__name__,
            )
            continue

    logger.info(
        "Gmail sync finished: checked=%s skipped=%s saved=%s failed=%s",
        stats["checked"],
        stats["skipped"],
        stats["saved"],
        stats["failed"],
    )
    return stats


def run_gmail_sync_once() -> dict[str, int]:
    """Manual one-shot sync for local testing (does not require the scheduler)."""
    logger.info("Running one-shot Gmail sync")
    return sync_gmail_inbox()


def _briefing_hour_utc() -> int:
    raw = os.getenv("BRIEFING_HOUR_UTC", str(DEFAULT_BRIEFING_HOUR_UTC))
    try:
        return min(23, max(0, int(raw)))
    except ValueError:
        return DEFAULT_BRIEFING_HOUR_UTC


def _briefing_minute_utc() -> int:
    raw = os.getenv("BRIEFING_MINUTE_UTC", str(DEFAULT_BRIEFING_MINUTE_UTC))
    try:
        return min(59, max(0, int(raw)))
    except ValueError:
        return DEFAULT_BRIEFING_MINUTE_UTC


def run_daily_briefing_job() -> dict:
    """Scheduler wrapper for daily briefing (isolates import/errors)."""
    from briefing_service import run_daily_briefing

    logger.info("Daily briefing job started")
    try:
        stats = run_daily_briefing()
        logger.info("Daily briefing job finished: %s", stats)
        return stats
    except Exception:
        logger.exception("Daily briefing job crashed")
        return {"email_count": 0, "saved": False, "briefing_id": None}


def run_daily_briefing_once() -> dict:
    """Manual one-shot briefing for local testing."""
    return run_daily_briefing_job()


def start_scheduler() -> None:
    global _scheduler

    _configure_scheduler_logging()

    if not scheduler_enabled():
        logger.info(
            "Scheduler disabled "
            "(set ENABLE_SCHEDULER=true to enable Gmail sync + daily briefing)"
        )
        return

    if _scheduler is not None and _scheduler.running:
        logger.warning("Scheduler already running; not starting another instance")
        return

    interval = _sync_interval_minutes()
    briefing_hour = _briefing_hour_utc()
    briefing_minute = _briefing_minute_utc()

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        sync_gmail_inbox,
        trigger=IntervalTrigger(minutes=interval),
        id="gmail_inbox_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    _scheduler.add_job(
        run_daily_briefing_job,
        trigger=CronTrigger(
            hour=briefing_hour,
            minute=briefing_minute,
            timezone="UTC",
        ),
        id="daily_email_briefing",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info(
        "Scheduler started: gmail_sync every %s min; "
        "daily_briefing at %02d:%02d UTC",
        interval,
        briefing_hour,
        briefing_minute,
    )


def stop_scheduler() -> None:
    global _scheduler

    _configure_scheduler_logging()

    if _scheduler is None:
        return

    try:
        if _scheduler.running:
            _scheduler.shutdown(wait=False)
            logger.info("Gmail scheduler stopped")
    except Exception:
        logger.exception("Error while stopping Gmail scheduler")
    finally:
        _scheduler = None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = run_gmail_sync_once()
    print(result)
