"""
Gmail access policy for this single-user EA.

Buddy may:
- read mail (gmail.readonly)
- send a new reply after explicit draft approval (gmail.send)
- read Google account name and photo (openid / userinfo) for the UI chip

Buddy must never:
- modify existing messages (labels, archive, mark read/unread)
- trash or delete messages or threads
- delete mail from Gmail, ever
- delete stored rows for the live protected mailbox
"""

from __future__ import annotations

import os
from typing import Any, Iterable

# Hard policy: this agent has no Gmail delete/trash authority.
NEVER_DELETE_GMAIL_MESSAGES = True
PROTECTED_GMAIL_ACCOUNT = "gaurang@rbhargavaassociates.in"
DEFAULT_ALLOWED_GMAIL_ACCOUNT = PROTECTED_GMAIL_ACCOUNT

ALLOWED_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
)

# Full mailbox / mutate scopes. Never request or accept these.
FORBIDDEN_SCOPES = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.settings.sharing",
    }
)

# Source-level ban: these Gmail API methods change or remove existing mail.
FORBIDDEN_GMAIL_API_FRAGMENTS = (
    "messages().trash",
    "messages().untrash",
    "messages().delete",
    "messages().batchDelete",
    "messages().modify",
    "messages().batchModify",
    "messages().update",
    "threads().trash",
    "threads().untrash",
    "threads().delete",
    "threads().modify",
    "labels().create",
    "labels().update",
    "labels().delete",
    "labels().patch",
)


class GmailAccessDenied(Exception):
    """Raised when the connected Google account or scopes are not allowed."""


def get_allowed_gmail_account() -> str:
    raw = (
        os.getenv("ALLOWED_GMAIL_ACCOUNT", DEFAULT_ALLOWED_GMAIL_ACCOUNT) or ""
    ).strip()
    return (raw or DEFAULT_ALLOWED_GMAIL_ACCOUNT).lower()


def get_protected_gmail_account() -> str:
    """Mailbox whose Gmail and local rows must never be deleted by this agent."""
    return PROTECTED_GMAIL_ACCOUNT.lower()


def assert_never_delete_gmail_messages() -> None:
    if not NEVER_DELETE_GMAIL_MESSAGES:
        raise GmailAccessDenied("Gmail delete policy is disabled; refusing to continue.")


def assert_can_purge_local_account(account: str | None) -> str:
    """Allow deleting leftover *local* rows only for a mailbox that is not Gaurang."""
    assert_never_delete_gmail_messages()
    cleaned = normalize_email(account)
    if not cleaned:
        raise GmailAccessDenied("Cannot purge an empty Gmail account key.")
    if cleaned == get_protected_gmail_account() or is_allowed_gmail_account(cleaned):
        raise GmailAccessDenied(
            f"Refusing to delete local data for protected mailbox {get_protected_gmail_account()}."
        )
    return cleaned


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def is_allowed_gmail_account(email: str | None) -> bool:
    return normalize_email(email) == get_allowed_gmail_account()


def assert_allowed_gmail_account(email: str | None) -> str:
    account = normalize_email(email)
    allowed = get_allowed_gmail_account()
    if account != allowed:
        raise GmailAccessDenied(
            f"Gmail account is not authorized for this assistant. "
            f"Connect {allowed} and retry at /auth/google."
        )
    return account


def _iter_scopes(scopes: Any) -> list[str]:
    if not scopes:
        return []
    if isinstance(scopes, str):
        return [part.strip() for part in scopes.replace(",", " ").split() if part.strip()]
    if isinstance(scopes, Iterable):
        return [str(item).strip() for item in scopes if str(item).strip()]
    return []


def find_forbidden_scopes(scopes: Any) -> list[str]:
    found: list[str] = []
    for scope in _iter_scopes(scopes):
        if scope in FORBIDDEN_SCOPES or scope not in ALLOWED_SCOPES:
            if scope not in found:
                found.append(scope)
    return found


def assert_gmail_scopes_safe(scopes: Any) -> None:
    forbidden = find_forbidden_scopes(scopes)
    if forbidden:
        raise GmailAccessDenied(
            "Gmail authorization includes disallowed permissions. "
            "Reconnect at /auth/google with read, send, and profile identity only."
        )


def assert_credentials_safe(credentials: Any) -> None:
    assert_gmail_scopes_safe(getattr(credentials, "scopes", None))
