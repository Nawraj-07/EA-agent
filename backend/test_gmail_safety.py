"""Gmail allowlist + no-modify/delete policy tests (no network)."""

from pathlib import Path
from unittest.mock import patch

import gmail_safety
from gmail_safety import (
    FORBIDDEN_GMAIL_API_FRAGMENTS,
    NEVER_DELETE_GMAIL_MESSAGES,
    PROTECTED_GMAIL_ACCOUNT,
    assert_allowed_gmail_account,
    assert_can_purge_local_account,
    assert_gmail_scopes_safe,
    assert_never_delete_gmail_messages,
    find_forbidden_scopes,
    is_allowed_gmail_account,
)


BACKEND_DIR = Path(__file__).resolve().parent


def test_allowlist_accepts_gaurang():
    with patch.dict(
        "os.environ",
        {"ALLOWED_GMAIL_ACCOUNT": "gaurang@rbhargavaassociates.in"},
        clear=False,
    ):
        assert is_allowed_gmail_account("Gaurang@RBHargavaAssociates.in")
        assert (
            assert_allowed_gmail_account("gaurang@rbhargavaassociates.in")
            == "gaurang@rbhargavaassociates.in"
        )


def test_allowlist_rejects_other_accounts():
    with patch.dict(
        "os.environ",
        {"ALLOWED_GMAIL_ACCOUNT": "gaurang@rbhargavaassociates.in"},
        clear=False,
    ):
        assert not is_allowed_gmail_account("developer@example.com")
        try:
            assert_allowed_gmail_account("developer@example.com")
            raise AssertionError("expected GmailAccessDenied")
        except gmail_safety.GmailAccessDenied:
            pass


def test_forbidden_modify_and_delete_scopes():
    forbidden = find_forbidden_scopes(
        [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        ]
    )
    assert "https://www.googleapis.com/auth/gmail.modify" in forbidden
    assert "https://mail.google.com/" in forbidden
    try:
        assert_gmail_scopes_safe(["https://www.googleapis.com/auth/gmail.modify"])
        raise AssertionError("expected GmailAccessDenied")
    except gmail_safety.GmailAccessDenied:
        pass


def test_never_delete_gmail_or_protected_mailbox():
    assert NEVER_DELETE_GMAIL_MESSAGES is True
    assert PROTECTED_GMAIL_ACCOUNT == "gaurang@rbhargavaassociates.in"
    assert_never_delete_gmail_messages()
    try:
        assert_can_purge_local_account("gaurang@rbhargavaassociates.in")
        raise AssertionError("expected GmailAccessDenied")
    except gmail_safety.GmailAccessDenied:
        pass
    assert (
        assert_can_purge_local_account("nawrajsingh2004@gmail.com")
        == "nawrajsingh2004@gmail.com"
    )


def test_readonly_send_and_identity_scopes_are_allowed():
    assert_gmail_scopes_safe(
        [
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    )


def test_backend_never_calls_gmail_mutate_or_delete_apis():
    hits: list[str] = []
    for path in BACKEND_DIR.glob("*.py"):
        if path.name.startswith("test_") or path.name == "gmail_safety.py":
            continue
        text = path.read_text(encoding="utf-8")
        for fragment in FORBIDDEN_GMAIL_API_FRAGMENTS:
            if fragment in text:
                hits.append(f"{path.name}: {fragment}")
    assert hits == []


if __name__ == "__main__":
    test_allowlist_accepts_gaurang()
    test_allowlist_rejects_other_accounts()
    test_forbidden_modify_and_delete_scopes()
    test_never_delete_gmail_or_protected_mailbox()
    test_readonly_send_and_identity_scopes_are_allowed()
    test_backend_never_calls_gmail_mutate_or_delete_apis()
    print("All gmail_safety tests passed.")
