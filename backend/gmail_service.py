
import base64
import logging
import re
from email.mime.text import MIMEText
from email.utils import parseaddr

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def extract_email_address(raw_sender: str | None) -> str | None:
    """Return a bare email address from a From header value, or None."""
    if not raw_sender or not str(raw_sender).strip():
        return None

    _name, address = parseaddr(str(raw_sender).strip())
    address = (address or str(raw_sender).strip()).strip().strip("<>")

    if _EMAIL_RE.fullmatch(address):
        return address.lower()

    return None


def format_reply_subject(subject: str | None) -> str:
    cleaned = (subject or "").strip() or "(no subject)"
    if cleaned.lower().startswith("re:"):
        return cleaned
    return f"Re: {cleaned}"


def _header_map(message: dict) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers") or []
    return {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in headers
        if header.get("name")
    }


def _fetch_reply_threading(
    service,
    original_gmail_message_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Return (thread_id, in_reply_to, references) for a Gmail message."""
    try:
        message = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=original_gmail_message_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References"],
            )
            .execute()
        )
    except HttpError as exc:
        logger.warning(
            "Could not load original Gmail message for threading (status=%s)",
            getattr(exc.resp, "status", "?"),
        )
        return None, None, None

    thread_id = message.get("threadId")
    headers = _header_map(message)
    message_id = headers.get("message-id") or None
    existing_refs = (headers.get("references") or "").strip()

    if message_id and existing_refs:
        references = f"{existing_refs} {message_id}".strip()
    else:
        references = existing_refs or message_id

    return thread_id, message_id, references


def send_gmail_reply(
    credentials: Credentials,
    *,
    to_email: str,
    subject: str,
    body: str,
    original_gmail_message_id: str | None = None,
) -> str:
    """
    Send a plain-text reply via Gmail and return the sent message id.

    Pass already-loaded/refreshed credentials from the app auth helpers.
    Never logs credential material.
    """
    recipient = extract_email_address(to_email)
    if not recipient:
        raise ValueError("Invalid recipient email address.")

    if not body or not str(body).strip():
        raise ValueError("Draft body cannot be empty.")

    service = build("gmail", "v1", credentials=credentials)

    thread_id = None
    in_reply_to = None
    references = None

    if original_gmail_message_id:
        thread_id, in_reply_to, references = _fetch_reply_threading(
            service,
            original_gmail_message_id,
        )

    message = MIMEText(str(body).strip(), "plain", "utf-8")
    message["To"] = recipient
    message["Subject"] = format_reply_subject(subject)

    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    send_body: dict = {"raw": raw_message}
    if thread_id:
        send_body["threadId"] = thread_id

    result = (
        service.users()
        .messages()
        .send(userId="me", body=send_body)
        .execute()
    )

    sent_id = result.get("id")
    if not sent_id:
        raise RuntimeError("Gmail send succeeded but returned no message id.")

    return str(sent_id)
