
"""
Gmail list / get / history helpers for inbox synchronization.

Ingest only — does not call OpenAI. Used by scheduler and inbox sync API.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
MAX_RETRIES = 5


class HistoryExpiredError(Exception):
    """Gmail historyId is too old; caller must re-backfill."""


def _batch_size() -> int:
    raw = os.getenv("GMAIL_SYNC_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
    try:
        return max(1, min(int(raw), 500))
    except ValueError:
        return DEFAULT_BATCH_SIZE


def _build_service(credentials: Any):
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _execute_with_backoff(request):
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as exc:
            last_exc = exc
            status = getattr(exc.resp, "status", None)
            if status in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES - 1:
                logger.warning(
                    "Gmail API transient error status=%s attempt=%s; backing off %.1fs",
                    status,
                    attempt + 1,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Gmail request failed")


def get_profile_email(credentials: Any) -> str:
    service = _build_service(credentials)
    profile = _execute_with_backoff(service.users().getProfile(userId="me"))
    email = (profile.get("emailAddress") or "").strip().lower()
    if not email:
        raise RuntimeError("Gmail profile missing emailAddress")
    return email


def get_profile_history_id(credentials: Any) -> str | None:
    service = _build_service(credentials)
    profile = _execute_with_backoff(service.users().getProfile(userId="me"))
    hid = profile.get("historyId")
    return str(hid) if hid is not None else None


def list_messages_page(
    credentials: Any,
    *,
    label_ids: list[str] | None = None,
    page_token: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    """
    One page of users.messages.list.

    Returns {message_ids, next_page_token, result_size_estimate}.
    """
    service = _build_service(credentials)
    size = max_results or _batch_size()
    kwargs: dict[str, Any] = {
        "userId": "me",
        "maxResults": min(size, 500),
    }
    if label_ids:
        kwargs["labelIds"] = label_ids
    if page_token:
        kwargs["pageToken"] = page_token

    result = _execute_with_backoff(service.users().messages().list(**kwargs))
    ids = [m["id"] for m in (result.get("messages") or []) if m.get("id")]
    return {
        "message_ids": ids,
        "next_page_token": result.get("nextPageToken"),
        "result_size_estimate": result.get("resultSizeEstimate"),
    }


def _headers_map(payload: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in (payload.get("headers") or [])
        if "name" in h and "value" in h
    }


def fetch_message_for_ingest(credentials: Any, message_id: str) -> dict[str, Any]:
    """
    Full message normalized for DB ingest (no AI).

    Includes threadId, labelIds, internalDate, headers, snippet, body.
    """
    from main import extract_email_body

    service = _build_service(credentials)
    message = _execute_with_backoff(
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
    )

    payload = message.get("payload") or {}
    headers = _headers_map(payload)
    label_ids = list(message.get("labelIds") or [])
    internal_raw = message.get("internalDate")
    try:
        internal_date = int(internal_raw) if internal_raw is not None else None
    except (TypeError, ValueError):
        internal_date = None

    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": label_ids,
        "internal_date": internal_date,
        "in_inbox": "INBOX" in label_ids,
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
        "body": extract_email_body(payload),
    }


def list_history_page(
    credentials: Any,
    *,
    start_history_id: str,
    page_token: str | None = None,
) -> dict[str, Any]:
    """
    One page of users.history.list.

    Raises HistoryExpiredError when startHistoryId is no longer valid.
    """
    service = _build_service(credentials)
    kwargs: dict[str, Any] = {
        "userId": "me",
        "startHistoryId": start_history_id,
        "maxResults": 100,
    }
    if page_token:
        kwargs["pageToken"] = page_token

    try:
        result = _execute_with_backoff(service.users().history().list(**kwargs))
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        # Gmail returns 404 when historyId is too old / invalid.
        if status == 404:
            raise HistoryExpiredError(str(exc)) from exc
        raise

    return {
        "history": result.get("history") or [],
        "next_page_token": result.get("nextPageToken"),
        "history_id": (
            str(result["historyId"]) if result.get("historyId") is not None else None
        ),
    }


def parse_history_events(history_records: list[dict]) -> dict[str, Any]:
    """
    Collapse history records into upsert / remove-from-inbox message id sets.
    """
    added: set[str] = set()
    removed: set[str] = set()
    label_updates: dict[str, set[str]] = {}

    for record in history_records:
        for item in record.get("messagesAdded") or []:
            msg = item.get("message") or {}
            mid = msg.get("id")
            if mid:
                added.add(mid)
                removed.discard(mid)

        for item in record.get("messagesDeleted") or []:
            msg = item.get("message") or {}
            mid = msg.get("id")
            if mid:
                removed.add(mid)
                added.discard(mid)

        for item in record.get("labelsRemoved") or []:
            msg = item.get("message") or {}
            mid = msg.get("id")
            labels = item.get("labelIds") or []
            if mid and "INBOX" in labels:
                removed.add(mid)
                added.discard(mid)

        for item in record.get("labelsAdded") or []:
            msg = item.get("message") or {}
            mid = msg.get("id")
            labels = item.get("labelIds") or []
            if mid and "INBOX" in labels:
                added.add(mid)
                removed.discard(mid)
            if mid and labels:
                label_updates.setdefault(mid, set()).update(labels)

    return {
        "added_ids": sorted(added),
        "removed_ids": sorted(removed),
        "label_touch_ids": sorted(label_updates.keys()),
    }


CATEGORY_LABELS = {
    "primary": "CATEGORY_PERSONAL",
    "promotions": "CATEGORY_PROMOTIONS",
    "social": "CATEGORY_SOCIAL",
}


def message_matches_category(label_ids: list[str] | None, category: str) -> bool:
    labels = set(label_ids or [])
    if "INBOX" not in labels:
        return False
    if category == "inbox":
        return True
    required = CATEGORY_LABELS.get(category)
    if not required:
        return category == "primary" and "CATEGORY_PERSONAL" in labels
    return required in labels
