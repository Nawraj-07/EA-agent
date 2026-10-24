"""Fernet encryption for Gmail OAuth JSON. Never log plaintext or ciphertext."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "GMAIL_TOKEN_ENCRYPTION_KEY"
CURRENT_KEY_VERSION = 1


class TokenCryptoError(Exception):
    """Raised when the encryption key is missing or ciphertext cannot be used."""


def encryption_key_configured() -> bool:
    return bool((os.getenv(KEY_ENV) or "").strip())


def _fernet() -> Fernet:
    raw = (os.getenv(KEY_ENV) or "").strip()
    if not raw:
        raise TokenCryptoError("GMAIL_TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(raw.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise TokenCryptoError("GMAIL_TOKEN_ENCRYPTION_KEY is invalid") from exc


def encrypt_token_json(plaintext: str) -> str:
    if not isinstance(plaintext, str) or not plaintext.strip():
        raise TokenCryptoError("Cannot encrypt empty credentials")
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_token_json(ciphertext: str) -> str:
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        raise TokenCryptoError("Cannot decrypt empty credentials")
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise TokenCryptoError("Stored Gmail credentials could not be decrypted") from exc
