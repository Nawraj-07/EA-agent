"""Tests for encrypted Gmail token storage. Fake credentials only.

Never read, write, or stat backend/token.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

import gmail_token_store
import token_crypto
from token_crypto import decrypt_token_json, encrypt_token_json

FAKE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
FAKE_TOKEN_INFO = {
    "token": "ya29.fake-access-token",
    "refresh_token": "1//fake-refresh-token",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "fake-client-id.apps.googleusercontent.com",
    "client_secret": "fake-client-secret",
    "scopes": FAKE_SCOPES,
}


def _fake_key() -> str:
    return Fernet.generate_key().decode("utf-8")


def _fake_creds() -> Credentials:
    return Credentials.from_authorized_user_info(FAKE_TOKEN_INFO, scopes=FAKE_SCOPES)


def _env(tmp_key: str | None, mode: str, **extra: str) -> dict[str, str]:
    env = {
        "GMAIL_TOKEN_STORE": mode,
        "ALLOWED_GMAIL_ACCOUNT": "gaurang@rbhargavaassociates.in",
        "SUPABASE_URL": extra.get("SUPABASE_URL", "https://example.supabase.co"),
        "SUPABASE_SERVICE_ROLE_KEY": extra.get(
            "SUPABASE_SERVICE_ROLE_KEY", "fake-service-role"
        ),
    }
    env["GMAIL_TOKEN_ENCRYPTION_KEY"] = tmp_key or ""
    env.update(extra)
    return env


def test_encrypt_decrypt_round_trip():
    key = _fake_key()
    payload = json.dumps(FAKE_TOKEN_INFO)
    with patch.dict("os.environ", {token_crypto.KEY_ENV: key}, clear=False):
        ciphertext = encrypt_token_json(payload)
        assert "fake-refresh-token" not in ciphertext
        assert decrypt_token_json(ciphertext) == payload


def test_encrypt_without_key_fails():
    with patch.dict("os.environ", {token_crypto.KEY_ENV: ""}, clear=False):
        try:
            encrypt_token_json(json.dumps(FAKE_TOKEN_INFO))
            raise AssertionError("expected TokenCryptoError")
        except token_crypto.TokenCryptoError:
            pass


def test_file_mode_round_trip():
    creds = _fake_creds()
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "fake-token.json"
        with patch.dict("os.environ", _env(None, "file"), clear=False):
            gmail_token_store.persist_credentials(creds, token_path=token_path)
            loaded = json.loads(
                gmail_token_store.load_credentials_payload(token_path=token_path)
            )
        assert loaded["refresh_token"] == "1//fake-refresh-token"
        assert token_path.is_file()


def test_file_mode_missing_raises():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "missing.json"
        with patch.dict("os.environ", _env(None, "file"), clear=False):
            try:
                gmail_token_store.load_credentials_payload(token_path=token_path)
                raise AssertionError("expected TokenStoreMissingError")
            except gmail_token_store.TokenStoreMissingError:
                pass


def test_supabase_mode_missing_key_fails():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "unused.json"
        with patch.dict("os.environ", _env(None, "supabase"), clear=False):
            try:
                gmail_token_store.load_credentials_payload(token_path=token_path)
                raise AssertionError("expected TokenStoreNotConfiguredError")
            except gmail_token_store.TokenStoreNotConfiguredError:
                pass
            try:
                gmail_token_store.persist_credentials(
                    _fake_creds(), token_path=token_path
                )
                raise AssertionError("expected TokenStoreNotConfiguredError")
            except gmail_token_store.TokenStoreNotConfiguredError:
                pass
        assert not token_path.exists()


def test_supabase_mode_never_writes_file():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "must-stay.json"
        token_path.write_text("do-not-change", encoding="utf-8")
        original = token_path.read_text(encoding="utf-8")
        key = _fake_key()
        upsert = MagicMock(
            return_value={"gmail_account_email": "gaurang@rbhargavaassociates.in"}
        )
        with patch.dict("os.environ", _env(key, "supabase"), clear=False):
            with patch("db.upsert_gmail_oauth_row", upsert):
                gmail_token_store.persist_credentials(
                    _fake_creds(), token_path=token_path
                )
        assert token_path.read_text(encoding="utf-8") == original
        assert upsert.called
        ciphertext = upsert.call_args.kwargs["ciphertext"]
        assert "fake-refresh-token" not in ciphertext


def test_supabase_mode_missing_row_raises():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "unused.json"
        key = _fake_key()
        with patch.dict("os.environ", _env(key, "supabase"), clear=False):
            with patch("db.get_gmail_oauth_row", return_value=None):
                try:
                    gmail_token_store.load_credentials_payload(token_path=token_path)
                    raise AssertionError("expected TokenStoreMissingError")
                except gmail_token_store.TokenStoreMissingError:
                    pass


def test_auto_falls_back_to_file():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "fake-token.json"
        token_path.write_text(json.dumps(FAKE_TOKEN_INFO), encoding="utf-8")
        with patch.dict(
            "os.environ",
            _env(None, "auto", SUPABASE_URL="", SUPABASE_SERVICE_ROLE_KEY=""),
            clear=False,
        ):
            loaded = json.loads(
                gmail_token_store.load_credentials_payload(token_path=token_path)
            )
        assert loaded["token"] == "ya29.fake-access-token"


def test_auto_prefers_supabase_over_file():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "fake-token.json"
        token_path.write_text(json.dumps({"token": "stale"}), encoding="utf-8")
        key = _fake_key()
        payload = json.dumps(FAKE_TOKEN_INFO)
        with patch.dict("os.environ", _env(key, "auto"), clear=False):
            ciphertext = encrypt_token_json(payload)
            with patch(
                "db.get_gmail_oauth_row",
                return_value={"ciphertext": ciphertext},
            ):
                loaded = gmail_token_store.load_credentials_payload(
                    token_path=token_path
                )
        assert json.loads(loaded)["refresh_token"] == "1//fake-refresh-token"


def test_auto_with_supabase_does_not_create_file():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "should-not-exist.json"
        key = _fake_key()
        upsert = MagicMock(return_value={"gmail_account_email": "x"})
        with patch.dict("os.environ", _env(key, "auto"), clear=False):
            with patch("db.upsert_gmail_oauth_row", upsert):
                gmail_token_store.persist_credentials(
                    _fake_creds(), token_path=token_path
                )
        assert not token_path.exists()
        assert upsert.called


def test_auto_dual_writes_existing_file():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "fake-token.json"
        token_path.write_text("{}", encoding="utf-8")
        key = _fake_key()
        upsert = MagicMock(return_value={"gmail_account_email": "x"})
        with patch.dict("os.environ", _env(key, "auto"), clear=False):
            with patch("db.upsert_gmail_oauth_row", upsert):
                gmail_token_store.persist_credentials(
                    _fake_creds(), token_path=token_path
                )
        saved = json.loads(token_path.read_text(encoding="utf-8"))
        assert saved["refresh_token"] == "1//fake-refresh-token"
        assert upsert.called


def test_unknown_mode_defaults_to_auto():
    with patch.dict("os.environ", {"GMAIL_TOKEN_STORE": "nope"}, clear=False):
        assert gmail_token_store.get_token_store_mode() == "auto"


def test_lock_is_reentrant():
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "fake-token.json"
        with patch.dict("os.environ", _env(None, "file"), clear=False):
            with gmail_token_store._lock:
                gmail_token_store.persist_credentials(
                    _fake_creds(), token_path=token_path
                )
                loaded = gmail_token_store.load_credentials_payload(
                    token_path=token_path
                )
        assert "fake-access-token" in loaded


if __name__ == "__main__":
    test_encrypt_decrypt_round_trip()
    test_encrypt_without_key_fails()
    test_file_mode_round_trip()
    test_file_mode_missing_raises()
    test_supabase_mode_missing_key_fails()
    test_supabase_mode_never_writes_file()
    test_supabase_mode_missing_row_raises()
    test_auto_falls_back_to_file()
    test_auto_prefers_supabase_over_file()
    test_auto_with_supabase_does_not_create_file()
    test_auto_dual_writes_existing_file()
    test_unknown_mode_defaults_to_auto()
    test_lock_is_reentrant()
    print("All gmail token store tests passed.")
