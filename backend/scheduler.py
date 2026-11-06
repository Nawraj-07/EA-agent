
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
from google.auth.exceptions import RefreshError
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
DEFAULT_INBOX_BACKFILL_AFTER = "2026/05/31"
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


def inbox_backfill_query() -> str:
    after = (
        os.getenv("GMAIL_INBOX_BACKFILL_AFTER", DEFAULT_INBOX_BACKFILL_AFTER).strip()
        or DEFAULT_INBOX_BACKFILL_AFTER
    )
    return f"in:inbox after:{after}"


def _backfill_token_prefix() -> str:
    after = (
        os.getenv("GMAIL_INBOX_BACKFILL_AFTER", DEFAULT_INBOX_BACKFILL_AFTER).strip()
        or DEFAULT_INBOX_BACKFILL_AFTER
    )
    return f"q{after.replace('/', '')}:"


def wrap_backfill_page_token(token: str | None) -> str | None:
    if not token:
        return None
    prefix = _backfill_token_prefix()
    if token.startswith(prefix):
        return token
    return f"{prefix}{token}"


def unwrap_backfill_page_token(token: str | None) -> tuple[str | None, bool]:
    """Return (gmail_page_token, stale). Stale tokens from a prior query are dropped."""
    if not token:
        return None, False
    prefix = _backfill_token_prefix()
    if token.startswith(prefix):
        return token[len(prefix) :] or None, False
    return None, True


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
        _ingest_message_ids(
            credentials, account, ids, stats, skip_existing=True
        )
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
        _ingest_message_ids(
            credentials, account, ids, stats, skip_existing=True
        )
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
        _ingest_message_ids(
            credentials, account, ids, stats, skip_existing=True
        )
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
    *,
    skip_existing: bool = False,
) -> None:
    """
    Fetch and upsert Gmail messages.

    skip_existing is for the newest, recent, unread, and backfill passes.
    Those passes list mail that is already stored. History additions and
    label touches still upsert so label changes are applied.
    """
    from egress_meter import using_operation

    ids = [mid for mid in message_ids if mid]
    if skip_existing and ids:
        try:
            with using_operation("gmail_existing_ids"):
                present = existing_gmail_message_ids(
                    ids, gmail_account_email=account
                )
        except Exception:
            logger.exception(
                "Could not check stored Gmail ids; ingesting the listed page"
            )
            present = set()
        fresh: list[str] = []
        for mid in ids:
            if mid in present:
                stats["checked"] = stats.get("checked", 0) + 1
                stats["skipped"] = stats.get("skipped", 0) + 1
            else:
                fresh.append(mid)
        ids = fresh

    with using_operation("gmail_upsert"):
        _upsert_listed_messages(credentials, account, ids, stats)


def _upsert_listed_messages(
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
                if _is_auth_failure(exc):
                    logger.warning(
                        "Gmail ingest paused for message_id=%s: authentication error type=%s",
                        message_id,
                        type(exc).__name__,
                    )
                    break
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
    raw_token = (state or {}).get("backfill_page_token")
    page_token, stale_token = unwrap_backfill_page_token(raw_token)
    if stale_token:
        logger.info(
            "Clearing backfill page token after Inbox query change account=%s",
            account,
        )
        upsert_sync_state(account, backfill_status="running", clear_page_token=True)
        page_token = None
    else:
        upsert_sync_state(account, backfill_status="running")

    query = inbox_backfill_query()
    pages = 0
    while pages < _max_backfill_pages():
        pages += 1
        page = list_messages_page(
            credentials,
            q=query,
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
        _ingest_message_ids(
            credentials, account, ids, stats, skip_existing=True
        )

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
            backfill_page_token=wrap_backfill_page_token(page_token),
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

    from egress_meter import using_operation

    with using_operation("analysis_queue"):
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
                analysis_data = analysis.model_dump()
                save_analysis_for_email(row["id"], analysis_data)
                stats["analyzed"] += 1
                try:
                    if str(analysis_data.get("importance") or "").lower() == "high":
                        from whatsapp_notifications import notify_whatsapp_important_email

                        notify_whatsapp_important_email(row, analysis_data)
                except Exception:
                    logger.exception(
                        "WhatsApp important-email notify failed email_id=%s "
                        "(analysis remains saved)",
                        row.get("id"),
                    )
            except Exception:
                stats["failed"] += 1
                try:
                    mark_analysis_failed(row["id"])
                except Exception:
                    logger.exception(
                        "Failed to mark analysis_status=failed id=%s",
                        row.get("id"),
                    )
                logger.exception(
                    "AI analysis failed email_id=%s (inbox row kept)",
                    row.get("id"),
                )
    return stats


def _is_auth_failure(exc: BaseException) -> bool:
    if isinstance(exc, RefreshError):
        return True
    if isinstance(exc, HttpError) and getattr(exc.resp, "status", None) in {401, 403}:
        return True
    return False


def run_analysis_queue() -> dict[str, int]:
    """Drain pending summaries for stored mail. Does not touch Gmail."""
    from egress_meter import record_job
    from fastapi import HTTPException
    from gmail_oauth import STATUS_RECONNECT_REQUIRED, get_gmail_auth_snapshot
    from main import load_gmail_credentials

    record_job("gmail_analysis_queue")

    empty = {"analyzed": 0, "failed": 0, "skipped": 0}
    snap = get_gmail_auth_snapshot()
    if snap.get("status") == STATUS_RECONNECT_REQUIRED:
        logger.info("Analysis queue skipped: Gmail reconnect required")
        return empty
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
    from egress_meter import record_job
    from gmail_oauth import set_gmail_syncing

    record_job("gmail_inbox_sync")
    set_gmail_syncing(True)
    try:
        return _sync_gmail_inbox_impl()
    finally:
        set_gmail_syncing(False)


def _sync_gmail_inbox_impl() -> dict[str, Any]:
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
        "status": "started",
    }

    try:
        credentials = load_gmail_credentials()
    except HTTPException as exc:
        detail = getattr(exc, "detail", None) or "Gmail authentication unavailable."
        logger.warning(
            "Gmail sync skipped: authentication unavailable (%s)",
            detail,
        )
        stats["status"] = "auth_unavailable"
        stats["error"] = str(detail)
        return stats
    except Exception:
        logger.exception("Gmail sync skipped: unexpected auth failure")
        stats["status"] = "auth_unavailable"
        stats["error"] = "Gmail authentication unavailable."
        return stats

    try:
        account = get_profile_email(credentials)
    except Exception:
        logger.exception("Gmail sync skipped: profile email unavailable")
        stats["status"] = "auth_unavailable"
        stats["error"] = "Gmail profile unavailable."
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
        status = getattr(exc.resp, "status", None)
        logger.warning("Gmail sync HttpError status=%s", status)
        from gmail_oauth import note_gmail_reconnect, note_gmail_temporary

        if status in {401, 403}:
            note_gmail_reconnect("gmail_http_unauthorized", "Gmail API rejected the access token.")
        elif status in {429, 500, 502, 503, 504}:
            note_gmail_temporary(
                "Gmail API temporarily failed. Sync will retry.",
                str(status),
            )
    except Exception:
        logger.exception("Gmail sync failed")
        from gmail_oauth import note_gmail_temporary

        note_gmail_temporary("Gmail sync hit an unexpected error. Sync will retry.")

    try:
        analyze_stats = analyze_pending_emails(
            gmail_account_email=account,
            statuses=["pending", "failed"],
        )
        stats["analyzed"] = analyze_stats.get("analyzed", 0)
        stats["analyze_failed"] = analyze_stats.get("failed", 0)
    except Exception:
        logger.exception("Analyze pass failed (inbox ingest already committed)")

    stats["status"] = stats.get("status") or "ok"

    # AI Follow-up Assistant: soft refresh after sync (no OpenAI, never blocks sync)
    try:
        from ai_followup_assistant import (
            ai_followup_assistant_enabled,
            refresh_followups_after_sync,
        )

        if ai_followup_assistant_enabled():
            followup_stats = refresh_followups_after_sync(account)
            stats["followup_refresh"] = followup_stats
    except Exception:
        logger.exception(
            "Follow-up refresh after sync failed; Gmail sync results preserved"
        )

    from egress_meter import instance_id

    logger.info(
        "Gmail sync finished account=%s mode=%s status=%s checked=%s saved=%s "
        "skipped=%s failed=%s archived=%s analyzed=%s analyze_failed=%s "
        "reconcile_scanned=%s reconcile_missing=%s instance=%s",
        account,
        stats["mode"],
        stats["status"],
        stats["checked"],
        stats["saved"],
        stats.get("skipped", 0),
        stats["failed"],
        stats["archived"],
        stats["analyzed"],
        stats["analyze_failed"],
        stats.get("reconcile_scanned", 0),
        stats.get("reconcile_missing", 0),
        instance_id(),
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
    from egress_meter import record_job

    record_job("daily_email_briefing")

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


def run_smart_mode_briefing_job(mode: str) -> dict:
    """Optional evening/weekly/monday job. Failures must not affect daily briefing."""
    try:
        from briefing_modes import compute_mode_period, mode_enabled, run_mode_briefing
        from briefing_service import _resolve_briefing_account

        if not mode_enabled(mode):
            logger.info("Smart mode job skipped (disabled) mode=%s", mode)
            return {"mode": mode, "status": "disabled", "saved": False}

        account = _resolve_briefing_account()
        if account:
            try:
                from db import try_claim_briefing_schedule

                period = compute_mode_period(mode)
                claimed = try_claim_briefing_schedule(
                    gmail_account_email=account,
                    job_kind=mode,
                    period_key=str(period.get("period_key") or ""),
                    metadata={"source": "env_mode_job"},
                )
                if claimed is None and period.get("period_key"):
                    logger.info(
                        "Smart mode job skipped (already claimed) mode=%s period=%s",
                        mode,
                        period.get("period_key"),
                    )
                    return {
                        "mode": mode,
                        "status": "already_claimed",
                        "saved": False,
                        "period_key": period.get("period_key"),
                    }
            except Exception:
                # Claim table may be missing until migration; continue safely.
                logger.exception(
                    "Schedule claim unavailable for mode=%s; continuing once", mode
                )

        stats = run_mode_briefing(mode, force=False)
        logger.info("Smart mode job finished mode=%s status=%s", mode, stats.get("status"))
        return stats
    except Exception:
        logger.exception("Smart mode job crashed mode=%s", mode)
        return {"mode": mode, "status": "job_crashed", "saved": False}


def run_personalized_mode_briefing_job(mode: str) -> dict:
    """
    Preference-driven mode job (Task 5). Separate job ids from env mode jobs.
    Morning/daily schedule is never overridden here.
    """
    try:
        from briefing_modes import compute_mode_period, run_mode_briefing
        from briefing_preferences import get_preferences_for_account, personalization_enabled
        from briefing_service import _resolve_briefing_account

        if not personalization_enabled():
            return {"mode": mode, "status": "personalization_disabled", "saved": False}
        if mode not in {"evening", "weekly", "monday"}:
            return {"mode": mode, "status": "unsupported", "saved": False}

        account = _resolve_briefing_account()
        if not account:
            return {"mode": mode, "status": "no_account", "saved": False}

        prefs = get_preferences_for_account(account).get("preferences") or {}
        schedule = (prefs.get("mode_schedules") or {}).get(mode) or {}
        if not schedule.get("enabled"):
            return {"mode": mode, "status": "schedule_disabled", "saved": False}

        period = compute_mode_period(mode)
        try:
            from db import try_claim_briefing_schedule

            claimed = try_claim_briefing_schedule(
                gmail_account_email=account,
                job_kind=mode,
                period_key=str(period.get("period_key") or ""),
                metadata={"source": "personalized_mode_job"},
            )
            if claimed is None and period.get("period_key"):
                return {
                    "mode": mode,
                    "status": "already_claimed",
                    "saved": False,
                    "period_key": period.get("period_key"),
                }
        except Exception:
            logger.exception(
                "Personalized schedule claim failed mode=%s; continuing once", mode
            )

        stats = run_mode_briefing(mode, force=False)
        stats["personalized"] = True
        return stats
    except Exception:
        logger.exception("Personalized mode job crashed mode=%s", mode)
        return {"mode": mode, "status": "job_crashed", "saved": False}


def _register_smart_briefing_mode_jobs(scheduler: BackgroundScheduler) -> None:
    """
    Authoritative auto path for Evening / Weekly / Monday (Task 3 env flags).

    Morning stays solely on daily_email_briefing — never register a second
    morning job here. Job ids: smart_briefing_{evening|weekly|monday}.
    """
    try:
        from briefing_modes import mode_enabled
        from briefing_service import get_briefing_tz

        zone = get_briefing_tz()
    except Exception:
        logger.exception("Could not load smart briefing mode scheduler helpers")
        return

    # Defaults: evening 17:00, weekly Saturday 17:00, monday 08:30 — local TZ.
    specs = (
        ("evening", CronTrigger(hour=17, minute=0, timezone=zone), "smart_briefing_evening"),
        (
            "weekly",
            CronTrigger(day_of_week="sat", hour=17, minute=0, timezone=zone),
            "smart_briefing_weekly",
        ),
        (
            "monday",
            CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=zone),
            "smart_briefing_monday",
        ),
    )
    for mode, trigger, job_id in specs:
        if not mode_enabled(mode):
            continue
        scheduler.add_job(
            run_smart_mode_briefing_job,
            trigger=trigger,
            kwargs={"mode": mode},
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("Registered smart briefing mode job id=%s mode=%s", job_id, mode)


def _register_personalized_briefing_jobs(scheduler: BackgroundScheduler) -> None:
    """
    Register preference-enabled mode schedules only.
    Does not alter daily_email_briefing. Does not register morning overrides.
    Skips modes already covered by env-enabled Task 3 jobs to avoid duplicates.
    """
    try:
        from briefing_modes import mode_enabled
        from briefing_preferences import (
            get_preferences_for_account,
            parse_schedule_hhmm,
            personalization_enabled,
        )
        from briefing_service import _resolve_briefing_account
        from zoneinfo import ZoneInfo
    except Exception:
        logger.exception("Could not load personalized briefing scheduler helpers")
        return

    if not personalization_enabled():
        return

    account = _resolve_briefing_account()
    if not account:
        logger.info("Personalized briefing jobs skipped (no authorized account)")
        return

    try:
        prefs = get_preferences_for_account(account).get("preferences") or {}
    except Exception:
        logger.exception("Could not load preferences for personalized jobs")
        return

    schedules = prefs.get("mode_schedules") or {}
    for mode in ("evening", "weekly", "monday"):
        # Prefer env Task-3 job when that flag is on — avoid duplicate generation paths.
        if mode_enabled(mode):
            continue
        schedule = schedules.get(mode) or {}
        if not schedule.get("enabled"):
            continue
        try:
            hour, minute = parse_schedule_hhmm(str(schedule.get("local_time") or "09:00"))
            tz_name = str(schedule.get("timezone") or prefs.get("timezone") or "Asia/Kolkata")
            zone = ZoneInfo(tz_name)
            weekday = schedule.get("weekday")
            trigger_kwargs: dict[str, Any] = {
                "hour": hour,
                "minute": minute,
                "timezone": zone,
            }
            if mode == "weekly" and weekday:
                trigger_kwargs["day_of_week"] = str(weekday)[:3]
            if mode == "monday":
                trigger_kwargs["day_of_week"] = "mon"
            job_id = f"personalized_briefing_{mode}"
            scheduler.add_job(
                run_personalized_mode_briefing_job,
                trigger=CronTrigger(**trigger_kwargs),
                kwargs={"mode": mode},
                id=job_id,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            logger.info(
                "Registered personalized briefing job id=%s mode=%s tz=%s",
                job_id,
                mode,
                tz_name,
            )
        except Exception:
            logger.exception("Failed registering personalized job mode=%s", mode)


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
        next_run_time=datetime.now(timezone.utc),
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
    try:
        from whatsapp_service import whatsapp_enabled

        if whatsapp_enabled():
            from whatsapp_notifications import flush_whatsapp_outbox

            _scheduler.add_job(
                flush_whatsapp_outbox,
                trigger=IntervalTrigger(minutes=5),
                id="whatsapp_outbox_flush",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
    except Exception:
        logger.exception("Could not register WhatsApp outbox flush job")

    _register_smart_briefing_mode_jobs(_scheduler)
    _register_personalized_briefing_jobs(_scheduler)

    _scheduler.start()
    from egress_meter import instance_id

    logger.info(
        "Scheduler started instance=%s gmail_sync every %s min; "
        "analysis queue every %s min; daily_briefing at %02d:%02d UTC",
        instance_id(),
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
