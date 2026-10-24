"""One-shot local migrator: encrypt token.json into Supabase.

Never prints credential JSON, ciphertext, or secrets.
Does not delete or overwrite token.json.
Do not run this in CI. Operator must pass --confirm.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials

from gmail_safety import assert_gmail_scopes_safe, get_allowed_gmail_account
from gmail_token_store import supabase_store_ready
from token_crypto import CURRENT_KEY_VERSION, encrypt_token_json

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_TOKEN_PATH = BACKEND_DIR / "token.json"

load_dotenv(BACKEND_DIR / ".env")
load_dotenv(BACKEND_DIR.parent / ".env", override=False)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Encrypt local Gmail OAuth JSON into Supabase. Never prints secrets."
    )
    parser.add_argument(
        "--from-file",
        dest="from_file",
        default=str(DEFAULT_TOKEN_PATH),
        help="Path to local token.json (default: backend/token.json)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Confirms you will not commit or paste token.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.confirm:
        print("migrated=no reason=confirm_required")
        return 2

    if not supabase_store_ready():
        print("migrated=no reason=store_not_configured")
        return 2

    token_path = Path(args.from_file)
    if not token_path.is_file():
        print("migrated=no reason=token_file_missing")
        return 1

    try:
        raw = token_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        print(f"migrated=no reason=invalid_file type={type(exc).__name__}")
        return 1

    if not isinstance(data, dict) or not data.get("refresh_token"):
        print("migrated=no reason=missing_refresh_token")
        return 1

    try:
        stored_scopes = data.get("scopes") or []
        assert_gmail_scopes_safe(stored_scopes)
        Credentials.from_authorized_user_info(data, scopes=stored_scopes)
    except Exception as exc:
        print(f"migrated=no reason=invalid_credentials type={type(exc).__name__}")
        return 1

    account = get_allowed_gmail_account()
    expiry = data.get("expiry") if isinstance(data.get("expiry"), str) else None

    try:
        from db import upsert_gmail_oauth_row

        ciphertext = encrypt_token_json(raw)
        upsert_gmail_oauth_row(
            account,
            ciphertext=ciphertext,
            key_version=CURRENT_KEY_VERSION,
            token_expiry=expiry,
        )
    except Exception as exc:
        print(f"migrated=no reason=upsert_failed type={type(exc).__name__}")
        return 1

    print(
        f"migrated=yes account={account} expiry={expiry or 'none'} source=file"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
