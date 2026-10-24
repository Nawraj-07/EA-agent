"""Load/save Gmail OAuth JSON via encrypted Supabase and/or local file.

Never log credential JSON, ciphertext, or encryption keys.
Never delete token.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from gmail_safety import get_allowed_gmail_account
from token_crypto import (
    CURRENT_KEY_VERSION,
    TokenCryptoError,
    decrypt_token_json,
    encrypt_token_json,
    encryption_key_configured,
)

STORE_ENV = "GMAIL_TOKEN_STORE"
ALLOWED_MODES = frozenset({"auto", "supabase", "file"})

logger = logging.getLogger(__name__)
_lock = threading.RLock()


class TokenStoreError(Exception):
    """Storage failed without exposing credential contents."""


class TokenStoreNotConfiguredError(TokenStoreError):
    """Supabase mode was requested but key or database credentials are missing."""


class TokenStoreMissingError(TokenStoreError):
    """No encrypted row and no local token file."""


def get_token_store_mode() -> str:
    raw = (os.getenv(STORE_ENV) or "auto").strip().lower()
    if raw not in ALLOWED_MODES:
        logger.warning("Unknown %s value; using auto", STORE_ENV)
        return "auto"
    return raw


def supabase_store_ready() -> bool:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    service_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    return bool(encryption_key_configured() and url and service_key)


def _expiry_iso(credentials) -> str | None:
    expiry = getattr(credentials, "expiry", None)
    if expiry is None:
        return None
    if isinstance(expiry, datetime):
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry.isoformat()
    return None


def persist_credentials(credentials, *, token_path: Path) -> None:
    payload = credentials.to_json()
    persist_payload(
        payload,
        token_path=token_path,
        token_expiry=_expiry_iso(credentials),
    )


def persist_payload(
    payload: str,
    *,
    token_path: Path,
    token_expiry: str | None = None,
) -> None:
    if not isinstance(payload, str) or not payload.strip():
        raise TokenStoreError("Cannot save empty Gmail credentials")
    try:
        json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise TokenStoreError("Gmail credentials JSON is invalid") from exc

    with _lock:
        _persist_unlocked(
            payload,
            token_path=token_path,
            token_expiry=token_expiry,
        )


def load_credentials_payload(*, token_path: Path) -> str:
    with _lock:
        return _load_unlocked(token_path=token_path)


def _save_supabase(payload: str, token_expiry: str | None) -> None:
    from db import upsert_gmail_oauth_row

    ciphertext = encrypt_token_json(payload)
    upsert_gmail_oauth_row(
        get_allowed_gmail_account(),
        ciphertext=ciphertext,
        key_version=CURRENT_KEY_VERSION,
        token_expiry=token_expiry,
    )
    logger.info("Gmail credentials saved to supabase")


def _write_file_if_allowed(payload: str, token_path: Path, *, create: bool) -> None:
    if create or token_path.is_file():
        token_path.write_text(payload, encoding="utf-8")
        logger.info("Gmail credentials saved to local file")


def _persist_unlocked(
    payload: str,
    *,
    token_path: Path,
    token_expiry: str | None,
) -> None:
    mode = get_token_store_mode()

    if mode == "supabase":
        if not supabase_store_ready():
            raise TokenStoreNotConfiguredError(
                "GMAIL_TOKEN_STORE=supabase requires GMAIL_TOKEN_ENCRYPTION_KEY "
                "and Supabase service credentials"
            )
        _save_supabase(payload, token_expiry)
        return

    if mode == "file":
        _write_file_if_allowed(payload, token_path, create=True)
        return

    supabase_ok = False
    if supabase_store_ready():
        try:
            _save_supabase(payload, token_expiry)
            supabase_ok = True
        except (TokenCryptoError, TokenStoreError, OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "Could not save Gmail credentials to supabase: %s",
                type(exc).__name__,
            )
            if token_path.is_file():
                _write_file_if_allowed(payload, token_path, create=False)
                return
            _write_file_if_allowed(payload, token_path, create=True)
            return

    if token_path.is_file():
        _write_file_if_allowed(payload, token_path, create=False)
        return

    if supabase_ok:
        return

    _write_file_if_allowed(payload, token_path, create=True)


def _load_from_supabase() -> str | None:
    from db import get_gmail_oauth_row

    row = get_gmail_oauth_row(get_allowed_gmail_account())
    ciphertext = (row or {}).get("ciphertext") if row else None
    if not ciphertext:
        return None
    return decrypt_token_json(str(ciphertext))


def _load_unlocked(*, token_path: Path) -> str:
    mode = get_token_store_mode()

    if mode == "supabase":
        if not supabase_store_ready():
            raise TokenStoreNotConfiguredError(
                "GMAIL_TOKEN_STORE=supabase requires GMAIL_TOKEN_ENCRYPTION_KEY "
                "and Supabase service credentials"
            )
        try:
            payload = _load_from_supabase()
        except TokenCryptoError:
            raise
        except Exception as exc:
            logger.warning(
                "Could not load Gmail credentials from supabase: %s",
                type(exc).__name__,
            )
            raise TokenStoreError("Unable to load Gmail credentials") from exc
        if not payload:
            raise TokenStoreMissingError("Gmail is not connected")
        return payload

    if mode == "file":
        if not token_path.is_file():
            raise TokenStoreMissingError("Gmail is not connected")
        return token_path.read_text(encoding="utf-8")

    if supabase_store_ready():
        try:
            payload = _load_from_supabase()
            if payload:
                return payload
        except TokenCryptoError:
            logger.warning(
                "Could not decrypt stored Gmail credentials: %s",
                TokenCryptoError.__name__,
            )
        except Exception as exc:
            logger.warning(
                "Could not load Gmail credentials from supabase: %s",
                type(exc).__name__,
            )

    if token_path.is_file():
        return token_path.read_text(encoding="utf-8")

    raise TokenStoreMissingError("Gmail is not connected")
