"""Gmail OAuth refresh classification and connection status.

Does not replace OAuth. Never logs tokens, ciphertext, or client secrets.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest

logger = logging.getLogger(__name__)

STATUS_CONNECTED = "connected"
STATUS_SYNCING = "syncing"
STATUS_RECONNECT_REQUIRED = "reconnect_required"
STATUS_TEMPORARY_ERROR = "temporary_sync_error"
STATUS_UNKNOWN = "unknown"

RECONNECT_ERROR_CODES = frozenset(
    {
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
    }
)
REFRESH_ATTEMPTS = 3
PERSIST_ATTEMPTS = 2

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": STATUS_UNKNOWN,
    "error_code": None,
    "detail": None,
    "syncing": False,
}


def extract_oauth_error(exc: BaseException) -> tuple[str | None, str | None]:
    """Return (error_code, description) with no credential material."""
    code: str | None = None
    desc: str | None = None
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict):
            raw_code = arg.get("error")
            raw_desc = arg.get("error_description") or arg.get("error_uri")
            if isinstance(raw_code, str) and raw_code.strip():
                code = raw_code.strip()
            if isinstance(raw_desc, str) and raw_desc.strip():
                desc = raw_desc.strip()[:180]
        elif isinstance(arg, str) and "invalid_grant" in arg.lower() and not code:
            code = "invalid_grant"
    text = str(exc).lower()
    if not code and "invalid_grant" in text:
        code = "invalid_grant"
    if not desc and "expired or revoked" in text:
        desc = "Token has been expired or revoked."
    return code, desc


def classify_refresh_failure(exc: BaseException) -> str:
    """Map a refresh/persist exception to reconnect_required or temporary_sync_error."""
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return STATUS_TEMPORARY_ERROR
    code, desc = extract_oauth_error(exc)
    text = f"{code or ''} {desc or ''} {exc}".lower()
    if code in RECONNECT_ERROR_CODES:
        return STATUS_RECONNECT_REQUIRED
    if "expired or revoked" in text or "invalid_grant" in text:
        return STATUS_RECONNECT_REQUIRED
    if isinstance(exc, RefreshError):
        return STATUS_RECONNECT_REQUIRED
    return STATUS_TEMPORARY_ERROR


def reconnect_detail(error_code: str | None, description: str | None) -> str:
    if (error_code or "").strip() == "invalid_grant":
        return (
            "Gmail refresh token was expired or revoked. "
            "Reconnect Gmail. If the Google Cloud OAuth app is in Testing, "
            "refresh tokens expire after 7 days until the app is published."
        )
    if description:
        return (
            "Gmail authentication failed. Reconnect Gmail. "
            f"{description}"
        )
    return "Gmail authentication failed. Reconnect Gmail."


def note_gmail_connected() -> None:
    with _lock:
        _state["status"] = STATUS_CONNECTED
        _state["error_code"] = None
        _state["detail"] = None


def note_gmail_reconnect(error_code: str | None, description: str | None = None) -> None:
    with _lock:
        _state["status"] = STATUS_RECONNECT_REQUIRED
        _state["error_code"] = error_code
        _state["detail"] = reconnect_detail(error_code, description)


def note_gmail_temporary(detail: str, error_code: str | None = None) -> None:
    with _lock:
        if _state["status"] == STATUS_RECONNECT_REQUIRED:
            return
        _state["status"] = STATUS_TEMPORARY_ERROR
        _state["error_code"] = error_code
        _state["detail"] = detail


def note_gmail_missing() -> None:
    note_gmail_reconnect(
        "not_connected",
        "Gmail is not connected.",
    )
    with _lock:
        _state["detail"] = "Gmail is not connected. Reconnect Gmail."


def set_gmail_syncing(active: bool) -> None:
    with _lock:
        _state["syncing"] = bool(active)


def get_gmail_auth_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def build_gmail_status_response(
    *,
    account: str,
    last_sync_at: str | None = None,
) -> dict[str, Any]:
    snap = get_gmail_auth_snapshot()
    status = snap.get("status") or STATUS_UNKNOWN
    if snap.get("syncing") and status not in {
        STATUS_RECONNECT_REQUIRED,
        STATUS_UNKNOWN,
    }:
        status = STATUS_SYNCING
    if status == STATUS_UNKNOWN:
        status = STATUS_TEMPORARY_ERROR
    return {
        "status": status,
        "account": account,
        "last_sync_at": last_sync_at,
        "error_code": snap.get("error_code"),
        "detail": snap.get("detail"),
        "reconnect_url": "/auth/google",
    }


def refresh_gmail_credentials(credentials, persist) -> None:
    """Refresh an expired access token and persist it.

    Reuses the stored refresh token. Retries transient network errors only.
    A revoked refresh token is not retried.
    """
    if not getattr(credentials, "refresh_token", None):
        note_gmail_reconnect("missing_refresh_token", "Gmail session expired.")
        raise RefreshError("missing_refresh_token")

    last_exc: BaseException | None = None
    for attempt in range(REFRESH_ATTEMPTS):
        try:
            credentials.refresh(GoogleRequest())
            _persist_refreshed(credentials, persist)
            note_gmail_connected()
            logger.info("Gmail access token refreshed")
            return
        except RefreshError as exc:
            last_exc = exc
            kind = classify_refresh_failure(exc)
            code, desc = extract_oauth_error(exc)
            if kind == STATUS_RECONNECT_REQUIRED:
                logger.warning(
                    "Gmail refresh token rejected error=%s",
                    code or "invalid_grant",
                )
                note_gmail_reconnect(code or "invalid_grant", desc)
                raise
            if attempt < REFRESH_ATTEMPTS - 1:
                delay = min(0.5 * (2 ** attempt), 4.0)
                logger.warning(
                    "Transient Gmail refresh error type=%s attempt=%s; retrying",
                    type(exc).__name__,
                    attempt + 1,
                )
                time.sleep(delay)
                continue
            note_gmail_temporary(
                "Gmail token refresh temporarily failed. Sync will retry.",
                code,
            )
            raise
        except (OSError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt < REFRESH_ATTEMPTS - 1:
                delay = min(0.5 * (2 ** attempt), 4.0)
                logger.warning(
                    "Transient Gmail refresh error type=%s attempt=%s; retrying",
                    type(exc).__name__,
                    attempt + 1,
                )
                time.sleep(delay)
                continue
            note_gmail_temporary(
                "Gmail token refresh temporarily failed. Sync will retry."
            )
            raise
    if last_exc:
        raise last_exc


def _persist_refreshed(credentials, persist) -> None:
    last_exc: BaseException | None = None
    for attempt in range(PERSIST_ATTEMPTS):
        try:
            persist(credentials)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < PERSIST_ATTEMPTS - 1:
                time.sleep(0.2)
                continue
    logger.warning(
        "Refreshed Gmail access token but could not persist credentials: %s",
        type(last_exc).__name__ if last_exc else "unknown",
    )
