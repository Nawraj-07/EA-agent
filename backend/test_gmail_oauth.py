"""Unit tests for Gmail OAuth refresh classification and status. No live tokens."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from google.auth.exceptions import RefreshError

import gmail_oauth
from gmail_oauth import (
    STATUS_CONNECTED,
    STATUS_RECONNECT_REQUIRED,
    STATUS_SYNCING,
    STATUS_TEMPORARY_ERROR,
    build_gmail_status_response,
    classify_refresh_failure,
    extract_oauth_error,
    refresh_gmail_credentials,
)


def setup_function():
    gmail_oauth.note_gmail_connected()
    gmail_oauth.set_gmail_syncing(False)
    gmail_oauth._state["status"] = gmail_oauth.STATUS_UNKNOWN
    gmail_oauth._state["error_code"] = None
    gmail_oauth._state["detail"] = None


def test_extract_invalid_grant():
    exc = RefreshError(
        "invalid_grant: Token has been expired or revoked.",
        {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        },
    )
    code, desc = extract_oauth_error(exc)
    assert code == "invalid_grant"
    assert "revoked" in (desc or "").lower()
    assert classify_refresh_failure(exc) == STATUS_RECONNECT_REQUIRED


def test_classify_timeout_as_temporary():
    assert classify_refresh_failure(TimeoutError("timed out")) == STATUS_TEMPORARY_ERROR
    assert classify_refresh_failure(OSError("network down")) == STATUS_TEMPORARY_ERROR


def test_refresh_does_not_retry_invalid_grant():
    creds = MagicMock()
    creds.refresh_token = "1//fake"
    creds.refresh.side_effect = RefreshError(
        "invalid_grant: Token has been expired or revoked.",
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
    )
    persist = MagicMock()
    try:
        refresh_gmail_credentials(creds, persist)
        raise AssertionError("expected RefreshError")
    except RefreshError:
        pass
    assert creds.refresh.call_count == 1
    assert persist.call_count == 0
    snap = gmail_oauth.get_gmail_auth_snapshot()
    assert snap["status"] == STATUS_RECONNECT_REQUIRED
    assert snap["error_code"] == "invalid_grant"


def test_refresh_retries_transient_then_succeeds():
    creds = MagicMock()
    creds.refresh_token = "1//fake"
    creds.refresh.side_effect = [OSError("temporarily unavailable"), None]
    persist = MagicMock()
    with patch("gmail_oauth.time.sleep"):
        refresh_gmail_credentials(creds, persist)
    assert creds.refresh.call_count == 2
    persist.assert_called_once_with(creds)
    assert gmail_oauth.get_gmail_auth_snapshot()["status"] == STATUS_CONNECTED


def test_refresh_keeps_in_memory_token_if_persist_fails():
    creds = MagicMock()
    creds.refresh_token = "1//fake"
    persist = MagicMock(side_effect=RuntimeError("supabase down"))
    with patch("gmail_oauth.time.sleep"):
        refresh_gmail_credentials(creds, persist)
    assert creds.refresh.call_count == 1
    assert persist.call_count == 2
    assert gmail_oauth.get_gmail_auth_snapshot()["status"] == STATUS_CONNECTED


def test_invalid_grant_reconnect_detail_is_explicit():
    detail = gmail_oauth.reconnect_detail(
        "invalid_grant",
        "Token has been expired or revoked.",
    )
    assert "Reconnect Gmail" in detail
    assert "expired or revoked" in detail.lower()


def test_status_overlay_syncing_and_reconnect():
    gmail_oauth.note_gmail_connected()
    gmail_oauth.set_gmail_syncing(True)
    payload = build_gmail_status_response(account="gaurang@rbhargavaassociates.in")
    assert payload["status"] == STATUS_SYNCING
    assert payload["reconnect_url"] == "/auth/google"

    gmail_oauth.note_gmail_reconnect("invalid_grant", "Token has been expired or revoked.")
    payload = build_gmail_status_response(account="gaurang@rbhargavaassociates.in")
    assert payload["status"] == STATUS_RECONNECT_REQUIRED
    assert "Reconnect Gmail" in (payload["detail"] or "") or "refresh token" in (
        payload["detail"] or ""
    ).lower()


def test_expired_access_token_is_not_reconnect_by_itself():
    """Access-token expiry is handled by refresh; only refresh failure reconnects."""
    assert classify_refresh_failure(TimeoutError()) != STATUS_RECONNECT_REQUIRED


if __name__ == "__main__":
    setup_function()
    test_extract_invalid_grant()
    setup_function()
    test_classify_timeout_as_temporary()
    setup_function()
    test_refresh_does_not_retry_invalid_grant()
    setup_function()
    test_refresh_retries_transient_then_succeeds()
    setup_function()
    test_refresh_keeps_in_memory_token_if_persist_fails()
    setup_function()
    test_invalid_grant_reconnect_detail_is_explicit()
    setup_function()
    test_status_overlay_syncing_and_reconnect()
    setup_function()
    test_expired_access_token_is_not_reconnect_by_itself()
    print("All gmail oauth tests passed.")
