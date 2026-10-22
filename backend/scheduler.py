
"""
Scheduled jobs (Tasks 6.1–6.2 + Gmail sync ordering).

- Gmail inbox sync: resumable INBOX backfill + History API incremental updates.
  Each run also covers newest pages, last 2 days, unread INBOX, and a
  reconcile pass so mail skipped by pagination still lands in Buddy.
  Ingest is separate from AI analysis. Gmail is never modified or deleted.
- Daily briefing: summarize recent stored emails into daily_briefings.

LIMITATIONS / PRODUCTION NOTES
------------------------------
- Designed for a single scheduler process. Do not run ENABLE_SCHEDULER=true
  on multiple Uvicorn workers or multiple deployed replicas without a
  distributed lock or a dedicated worker process.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from googleapiclient.errors import HttpError

from ai_service import analyze_email_content
from db import (
    existing_gmail_message_ids,
    get_sync_state,
    list_pending_analysis_emails,
    mark_analysis_failed,
    mark_email_not_in_inbox,
    save_analysis_for_email,
    upsert_email_record,
    upsert_sync_state,
)
from gmail_sync import (
    HistoryExpiredError,
    _batch_size,
    fetch_message_for_ingest,
    get_profile_email,
    get_profile_history_id,
    list_history_page,
    list_messages_page,
    parse_history_events,
)


logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_logging_configured = False

DEFAULT_SYNC_INTERVAL_MINUTES = 15
DEFAULT_ANALYZE_PER_RUN = 100
DEFAULT_ANALYZE_INTERVAL_MINUTES = 1
DEFAULT_BRIEFING_HOUR_UTC = 8
DEFAULT_BRIEFING_MINUTE_UTC = 0
DEFAULT_MAX_BACKFILL_PAGES = 3
DEFAULT_NEWEST_PAGES = 3
DEFAULT_RECENT_SYNC_PAGES = 5
DEFAULT_RECONCILE_PAGES = 10
INGEST_FETCH_RETRIES = 3


def _configure_scheduler_logging() -> None:
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


def _analyze_per_run() -> int:
    raw = os.getenv("GMAIL_ANALYZE_PER_RUN", str(DEFAULT_ANALYZE_PER_RUN))
    try:
        return max(0, min(int(raw), 2000))
    except ValueError:
        return DEFAULT_ANALYZE_PER_RUN


def _analyze_interval_minutes() -> int:
    raw = os.getenv(
        "GMAIL_ANALYZE_INTERVAL_MINUTES",
        str(DEFAULT_ANALYZE_INTERVAL_MINUTES),
    )
    try:
        return max(1, min(int(raw), 30))
    except ValueError:
        return DEFAULT_ANALYZE_INTERVAL_MINUTES


def _max_backfill_pages() -> int:
    raw = os.getenv("GMAIL_SYNC_MAX_BACKFILL_PAGES", str(DEFAULT_MAX_BACKFILL_PAGES))
    try:
        return max(1, min(int(raw), 50))
    except ValueError:
        return DEFAULT_MAX_BACKFILL_PAGES


def _newest_pages() -> int:
    raw = os.getenv("GMAIL_NEWEST_PAGES", str(DEFAULT_NEWEST_PAGES))
    try:
        return max(1, min(int(raw), 10))
    except ValueError:
        return DEFAULT_NEWEST_PAGES


def _recent_sync_pages() -> int:
    raw = os.getenv("GMAIL_RECENT_SYNC_PAGES", str(DEFAULT_RECENT_SYNC_PAGES))
    try:
        return max(1, min(int(raw), 20))
    except ValueError:
        return DEFAULT_RECENT_SYNC_PAGES


def _reconcile_pages() -> int:
    raw = os.getenv("GMAIL_RECONCILE_PAGES", str(DEFAULT_RECONCILE_PAGES))
    try:
        return max(1, min(int(raw), 50))
    except ValueError:
        return DEFAULT_RECONCILE_PAGES


def ingest_newest_inbox(
    credentials: Any,
    account: str,
    stats: dict[str, Any],
    *,
    pages: int | None = None,
) -> None:
    """Ingest the newest INBOX page(s) so the top of Buddy matches Gmail."""
    page_token: str | None = None
    for _ in range(pages if pages is not None else _newest_pages()):
        page = list_messages_page(
            credentials,
            label_ids=["INBOX"],
            page_token=page_token,
            max_results=_batch_size(),
        )
        ids = page["message_ids"]
        if not ids:
            break
        logger.info(
            "Gmail newest-inbox ingest count=%s account=%s",
            len(ids),
            account,
        )
        _ingest_message_ids(credentials, account, ids, stats)
        page_token = page.get("next_page_token")
        if not page_token:
            break


def ingest_inbox_for_local_day(
    credentials: Any,
    account: str,
    day,
    stats: dict[str, Any] | None = None,
    *,
    max_pages: int = 8,
) -> dict[str, Any]:
    """Read-only fetch of INBOX messages for one local calendar day, then upsert."""
    from datetime import timedelta

    stats = stats or {
        "checked": 0,
        "saved": 0,
        "failed": 0,
        "account": account,
        "day": str(day),
    }
    nxt = day + timedelta(days=1)
    query = (
        f"in:inbox after:{day.strftime('%Y/%m/%d')} "
        f"before:{nxt.strftime('%Y/%m/%d')}"
    )
    page_token: str | None = None
    for _ in range(max(1, max_pages)):
        page = list_messages_page(
            credentials,
            q=query,
            page_token=page_token,
            max_results=_batch_size(),
        )
        ids = page["message_ids"]
        logger.info(
            "Gmail day ingest date=%s count=%s account=%s",
            day.isoformat() if hasattr(day, "isoformat") else day,
            len(ids),
            account,
        )
        if not ids:
            break
        _ingest_message_ids(credentials, account, ids, stats)
        page_token = page.get("next_page_token")
        if not page_token:
            break
    return stats


def ingest_recent_inbox(
    credentials: Any,
    account: str,
    stats: dict[str, Any],
    *,
    query: str = "in:inbox newer_than:2d",
    max_pages: int | None = None,
) -> None:
    """
    Catch mail from the last two days that pagination skipped during backfill.

    Read-only Gmail list/get. Does not modify or delete messages.
    """
    page_token: str | None = None
    limit = max_pages if max_pages is not None else _recent_sync_pages()
    for _ in range(max(1, limit)):
        page = list_messages_page(
            credentials,
            q=query,
            page_token=page_token,
            max_results=_batch_size(),
        )
        ids = page["message_ids"]
        logger.info(
            "Gmail recent-inbox ingest count=%s account=%s",
            len(ids),
            account,
        )
        if not ids:
            break
        _ingest_message_ids(credentials, account, ids, stats)
        page_token = page.get("next_page_token")
        if not page_token:
            break


def reconcile_missing_inbox(
    credentials: Any,
    account: str,
    stats: dict[str, Any],
    *,
    max_pages: int | None = None,
) -> None:
    """
    Compare Gmail INBOX ids to local rows and ingest any gaps.

    Covers mail that pagination skipped. Never deletes Gmail or local Gaurang rows.
    """
    page_token: str | None = None
    missing: list[str] = []
    scanned = 0
    limit = max_pages if max_pages is not None else _reconcile_pages()

    for _ in range(max(1, limit)):
        page = list_messages_page(
            credentials,
            label_ids=["INBOX"],
            page_token=page_token,
            max_results=_batch_size(),
        )
        ids = page["message_ids"]
        if not ids:
            break
        scanned += len(ids)
        present = existing_gmail_message_ids(ids, gmail_account_email=account)
        missing.extend(mid for mid in ids if mid not in present)
        page_token = page.get("next_page_token")
        if not page_token:
            break

    stats["reconcile_scanned"] = scanned
    stats["reconcile_missing"] = len(missing)
    logger.info(
        "Gmail inbox reconcile scanned=%s missing=%s account=%s",
        scanned,
        len(missing),
        account,
    )
    if missing:
        _ingest_message_ids(credentials, account, missing, stats)


def _ingest_message_ids(
    credentials: Any,
    account: str,
    message_ids: list[str],
    stats: dict[str, Any],
) -> None:
    for message_id in message_ids:
        stats["checked"] += 1
        last_exc: Exception | None = None
        for attempt in range(INGEST_FETCH_RETRIES):
            try:
                email = fetch_message_for_ingest(credentials, message_id)
                upsert_email_record(email, gmail_account_email=account)
                stats["saved"] += 1
                last_exc = None
                logger.info(
                    "Gmail ingest upserted message_id=%s account=%s in_inbox=%s",
                    message_id,
                    account,
                    email.get("in_inbox"),
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt < INGEST_FETCH_RETRIES - 1:
                    time.sleep(min(0.5 * (2 ** attempt), 4.0))
        if last_exc is not None:
            stats["failed"] += 1
            logger.error(
                "Gmail ingest failed message_id=%s account=%s",
                message_id,
                account,
                exc_info=last_exc,
            )


def _run_backfill(
    credentials: Any,
    account: str,
    state: dict | None,
    stats: dict[str, Any],
) -> None:
    page_token = (state or {}).get("backfill_page_token")
    upsert_sync_state(account, backfill_status="running")

    pages = 0
    while pages < _max_backfill_pages():
        pages += 1
        page = list_messages_page(
            credentials,
            label_ids=["INBOX"],
            page_token=page_token,
            max_results=_batch_size(),
        )
        ids = page["message_ids"]
        stats["pages"] += 1
        logger.info(
            "Gmail backfill page=%s count=%s account=%s",
            pages,
            len(ids),
            account,
        )
        _ingest_message_ids(credentials, account, ids, stats)

        page_token = page.get("next_page_token")
        if not page_token:
            history_id = get_profile_history_id(credentials)
            upsert_sync_state(
                account,
                history_id=history_id,
                backfill_status="complete",
                clear_page_token=True,
            )
            stats["backfill_complete"] = True
            logger.info(
                "Gmail backfill complete account=%s history_id=%s",
                account,
                history_id,
            )
            return

        upsert_sync_state(
            account,
            backfill_page_token=page_token,
            backfill_status="running",
        )

    stats["backfill_complete"] = False
    logger.info(
        "Gmail backfill paused after %s pages; will resume next run account=%s",
        pages,
        account,
    )


def _run_history_incremental(
    credentials: Any,
    account: str,
    start_history_id: str,
    stats: dict[str, Any],
    *,
    preserve_backfill: bool = False,
) -> None:
    page_token: str | None = None
    latest_history_id = start_history_id

    while True:
        page = list_history_page(
            credentials,
            start_history_id=start_history_id,
            page_token=page_token,
        )
        events = parse_history_events(page["history"])
        stats["history_pages"] += 1

        for mid in events["removed_ids"]:
            mark_email_not_in_inbox(mid, gmail_account_email=account)
            stats["archived"] += 1

        _ingest_message_ids(credentials, account, events["added_ids"], stats)

        # Refresh labels for messages that only had label changes (not in added).
        touch_ids = [
            mid
            for mid in events["label_touch_ids"]
            if mid not in set(events["added_ids"])
            and mid not in set(events["removed_ids"])
        ]
        _ingest_message_ids(credentials, account, touch_ids, stats)

        if page.get("history_id"):
            latest_history_id = page["history_id"]

        page_token = page.get("next_page_token")
        if not page_token:
            break

    if preserve_backfill:
        upsert_sync_state(account, history_id=latest_history_id)
    else:
        upsert_sync_state(
            account,
            history_id=latest_history_id,
            backfill_status="complete",
            clear_page_token=True,
        )
    logger.info(
        "Gmail history sync done account=%s history_id=%s archived=%s preserve_backfill=%s",
        account,
        latest_history_id,
        stats["archived"],
        preserve_backfill,
    )


def analyze_pending_emails(
    *,
    gmail_account_email: str,
    limit: int | None = None,
    statuses: list[str] | None = None,
) -> dict[str, int]:
    """Run AI analysis for pending/failed rows without blocking ingest."""
    cap = _analyze_per_run() if limit is None else limit
    stats = {"analyzed": 0, "failed": 0, "skipped": 0}
    if cap <= 0:
        return stats

    rows = list_pending_analysis_emails(
        gmail_account_email=gmail_account_email,
        limit=cap,
        statuses=statuses,
    )
    for row in rows:
        try:
            analysis = analyze_email_content(
                subject=row.get("subject") or "",
                sender=row.get("sender") or "",
                body=row.get("body") or "",
            )
            save_analysis_for_email(row["id"], analysis.model_dump())
            stats["analyzed"] += 1
        except Exception:
            stats["failed"] += 1
            try:
                mark_analysis_failed(row["id"])
            except Exception:
                logger.exception("Failed to mark analysis_status=failed id=%s", row.get("id"))
            logger.exception(
                "AI analysis failed email_id=%s (inbox row kept)",
                row.get("id"),
            )
    return stats


def run_analysis_queue() -> dict[str, int]:
    """Drain pending summaries for stored mail. Does not touch Gmail."""
    from fastapi import HTTPException
    from main import load_gmail_credentials

    empty = {"analyzed": 0, "failed": 0, "skipped": 0}
    try:
        credentials = load_gmail_credentials()
        account = get_profile_email(credentials)
    except HTTPException:
        logger.info("Analysis queue skipped: Gmail not connected")
        return empty
    except Exception:
        logger.exception("Analysis queue skipped: account unavailable")
        return empty

    logger.info("Analysis queue started account=%s", account)
    stats = analyze_pending_emails(
        gmail_account_email=account,
        statuses=["pending"],
    )
    logger.info(
        "Analysis queue finished account=%s analyzed=%s failed=%s",
        account,
        stats["analyzed"],
        stats["failed"],
    )
    return stats


def sync_gmail_inbox() -> dict[str, Any]:
    """
    Sync Gmail INBOX into Supabase (ingest first, then optional analyze).

    Safe when Gmail auth is missing/expired: logs and returns without raising.
    """
    from fastapi import HTTPException
    from main import load_gmail_credentials

    stats: dict[str, Any] = {
        "checked": 0,
        "skipped": 0,
        "saved": 0,
        "failed": 0,
        "archived": 0,
        "pages": 0,
        "history_pages": 0,
        "backfill_complete": False,
        "mode": None,
        "analyzed": 0,
        "analyze_failed": 0,
        "account": None,
        "reconcile_scanned": 0,
        "reconcile_missing": 0,
    }

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

    try:
        account = get_profile_email(credentials)
    except Exception:
        logger.exception("Gmail sync skipped: profile email unavailable")
        return stats

    stats["account"] = account
    state = get_sync_state(account) or {}
    backfill_status = state.get("backfill_status") or "pending"

    if not state.get("history_id"):
        live_history_id = get_profile_history_id(credentials)
        if live_history_id:
            upsert_sync_state(account, history_id=live_history_id)
            state = get_sync_state(account) or state

    try:
        ingest_newest_inbox(credentials, account, stats)
    except Exception:
        logger.exception("Newest-inbox ingest failed; continuing sync")

    try:
        ingest_recent_inbox(credentials, account, stats)
    except Exception:
        logger.exception("Recent-inbox ingest failed; continuing sync")

    try:
        ingest_recent_inbox(
            credentials,
            account,
            stats,
            query="in:inbox is:unread",
            max_pages=3,
        )
    except Exception:
        logger.exception("Unread-inbox ingest failed; continuing sync")

    try:
        reconcile_missing_inbox(credentials, account, stats)
    except Exception:
        logger.exception("Inbox reconcile failed; continuing sync")

    history_id = state.get("history_id")
    if history_id and backfill_status != "complete":
        try:
            _run_history_incremental(
                credentials,
                account,
                history_id,
                stats,
                preserve_backfill=True,
            )
            state = get_sync_state(account) or state
            history_id = state.get("history_id") or history_id
        except HistoryExpiredError:
            logger.warning(
                "Gmail history expired during backfill account=%s; continuing backfill",
                account,
            )
        except Exception:
            logger.exception("History catch-up during backfill failed; continuing")

    try:
        if backfill_status != "complete":
            stats["mode"] = "backfill"
            _run_backfill(credentials, account, state, stats)
        else:
            history_id = (get_sync_state(account) or {}).get("history_id") or history_id
            if not history_id:
                stats["mode"] = "backfill"
                upsert_sync_state(account, backfill_status="pending", clear_page_token=True)
                _run_backfill(credentials, account, get_sync_state(account), stats)
            else:
                stats["mode"] = "history"
                try:
                    _run_history_incremental(credentials, account, history_id, stats)
                except HistoryExpiredError:
                    logger.warning(
                        "Gmail history expired account=%s; restarting backfill",
                        account,
                    )
                    stats["mode"] = "backfill_recovery"
                    upsert_sync_state(
                        account,
                        backfill_status="pending",
                        clear_page_token=True,
                    )
                    _run_backfill(credentials, account, get_sync_state(account), stats)
    except HttpError as exc:
        logger.warning(
            "Gmail sync HttpError status=%s",
            getattr(exc.resp, "status", "?"),
        )
    except Exception:
        logger.exception("Gmail sync failed")

    try:
        analyze_stats = analyze_pending_emails(
            gmail_account_email=account,
            statuses=["pending", "failed"],
        )
        stats["analyzed"] = analyze_stats.get("analyzed", 0)
        stats["analyze_failed"] = analyze_stats.get("failed", 0)
    except Exception:
        logger.exception("Analyze pass failed (inbox ingest already committed)")

    logger.info(
        "Gmail sync finished account=%s mode=%s checked=%s saved=%s "
        "failed=%s archived=%s analyzed=%s analyze_failed=%s "
        "reconcile_scanned=%s reconcile_missing=%s",
        account,
        stats["mode"],
        stats["checked"],
        stats["saved"],
        stats["failed"],
        stats["archived"],
        stats["analyzed"],
        stats["analyze_failed"],
        stats.get("reconcile_scanned", 0),
        stats.get("reconcile_missing", 0),
    )
    return stats


def run_gmail_sync_once() -> dict[str, Any]:
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


def run_daily_briefing_job(target_date=None) -> dict:
    from briefing_service import run_daily_briefing

    logger.info("Daily briefing job started target_date=%s", target_date)
    try:
        stats = run_daily_briefing(target_date=target_date)
        logger.info("Daily briefing job finished: %s", stats)
        return stats
    except Exception:
        logger.exception("Daily briefing job crashed")
        return {"email_count": 0, "saved": False, "briefing_id": None}


def run_daily_briefing_once(target_date=None) -> dict:
    return run_daily_briefing_job(target_date=target_date)


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
    analyze_interval = _analyze_interval_minutes()
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
        run_analysis_queue,
        trigger=IntervalTrigger(minutes=analyze_interval),
        id="gmail_analysis_queue",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=datetime.now(timezone.utc),
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
        "analysis queue every %s min; daily_briefing at %02d:%02d UTC",
        interval,
        analyze_interval,
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
