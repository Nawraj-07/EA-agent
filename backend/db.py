import os
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env", override=False)


def get_supabase_client():
    url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not service_role_key:
        raise RuntimeError("Supabase credentials are missing")

    return create_client(url, service_role_key)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def email_exists_by_gmail_id(gmail_message_id: str) -> bool:
    """Return True if this Gmail message is already stored (unique gmail_message_id)."""
    if not gmail_message_id:
        return False

    client = get_supabase_client()

    response = (
        client.table("emails")
        .select("id")
        .eq("gmail_message_id", gmail_message_id)
        .limit(1)
        .execute()
    )

    return bool(response.data)


def save_email_and_analysis(email: dict, analysis: dict) -> None:
    client = get_supabase_client()

    email_data = {
        "gmail_message_id": email["id"],
        "subject": email.get("subject", ""),
        "sender": email.get("from", ""),
        "received_at": email.get("date", ""),
        "snippet": email.get("snippet", ""),
        "body": email.get("body", "")
    }

    email_result = (
        client.table("emails")
        .upsert(email_data, on_conflict="gmail_message_id")
        .execute()
    )

    if not email_result.data:
        raise RuntimeError("Failed to save email")

    email_id = email_result.data[0]["id"]

    analysis_data = {
        "email_id": email_id,
        "summary": analysis["summary"],
        "category": analysis["category"],
        "importance": analysis["importance"],
        "deadlines": analysis["deadlines"],
        "action_required": analysis["action_required"],
        "confidence": analysis["confidence"]
    }

    analysis_result = (
        client.table("email_analyses")
        .upsert(analysis_data, on_conflict="email_id")
        .execute()
    )

    if not analysis_result.data:
        raise RuntimeError("Failed to save email analysis")


def list_stored_emails(limit=20):
    client = get_supabase_client()

    response = (
        client.table("emails")
        .select("*, email_analyses(*)")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    return response.data


def get_stored_email(email_id: str):
    client = get_supabase_client()

    response = (
        client.table("emails")
        .select("*, email_analyses(*)")
        .eq("id", email_id)
        .maybe_single()
        .execute()
    )

    return response.data


def save_draft(email_id: str, body: str) -> dict:
    client = get_supabase_client()

    draft_data = {
        "email_id": email_id,
        "body": body,
        "status": "pending",
    }

    result = (
        client.table("drafts")
        .insert(draft_data)
        .execute()
    )

    if not result.data:
        raise RuntimeError("Failed to save draft")

    return result.data[0]


def get_draft(draft_id: str):
    client = get_supabase_client()

    response = (
        client.table("drafts")
        .select("*")
        .eq("id", draft_id)
        .maybe_single()
        .execute()
    )

    return response.data


def update_draft(draft_id: str, updates: dict):
    client = get_supabase_client()

    response = (
        client.table("drafts")
        .update(updates)
        .eq("id", draft_id)
        .eq("status", "pending")
        .execute()
    )

    return response.data[0] if response.data else None


def get_draft_with_email(draft_id: str):
    client = get_supabase_client()

    response = (
        client.table("drafts")
        .select(
            "*, emails(id, subject, sender, gmail_message_id, snippet, body)"
        )
        .eq("id", draft_id)
        .maybe_single()
        .execute()
    )

    return response.data


def claim_draft_for_sending(draft_id: str):
    """
    Atomically claim a pending draft for send (pending → sending).

    Uses a single conditional UPDATE (not an RPC). Concurrent callers race on
    WHERE status = 'pending'; only one update matches and wins.
    """
    client = get_supabase_client()

    response = (
        client.table("drafts")
        .update({
            "status": "sending",
            "updated_at": _utcnow(),
        })
        .eq("id", draft_id)
        .eq("status", "pending")
        .select("*")
        .execute()
    )

    return response.data[0] if response.data else None


def release_draft_claim(draft_id: str):
    """Return a claimed draft to pending after a definite pre-send/send failure."""
    client = get_supabase_client()

    response = (
        client.table("drafts")
        .update({
            "status": "pending",
            "updated_at": _utcnow(),
        })
        .eq("id", draft_id)
        .eq("status", "sending")
        .select("*")
        .execute()
    )

    return response.data[0] if response.data else None


def mark_draft_sent(draft_id: str, gmail_sent_message_id: str | None = None):
    """Mark a claimed draft as sent after Gmail confirms success."""
    client = get_supabase_client()

    updates = {
        "status": "sent",
        "updated_at": _utcnow(),
    }

    if gmail_sent_message_id:
        updates["gmail_sent_message_id"] = gmail_sent_message_id

    response = (
        client.table("drafts")
        .update(updates)
        .eq("id", draft_id)
        .eq("status", "sending")
        .select("*")
        .execute()
    )

    return response.data[0] if response.data else None


def list_emails_for_briefing(since_iso: str, limit: int = 30) -> list:
    """Recent stored emails (with analyses) for the daily briefing window."""
    client = get_supabase_client()

    response = (
        client.table("emails")
        .select("*, email_analyses(*)")
        .gte("created_at", since_iso)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    return response.data or []


def save_briefing(
    *,
    content: dict,
    period_start: str,
    period_end: str,
    email_count: int,
) -> dict:
    client = get_supabase_client()

    row = {
        "content": content,
        "period_start": period_start,
        "period_end": period_end,
        "email_count": email_count,
    }

    result = client.table("daily_briefings").insert(row).execute()

    if not result.data:
        raise RuntimeError("Failed to save daily briefing")

    return result.data[0]


def get_latest_briefing():
    client = get_supabase_client()

    response = (
        client.table("daily_briefings")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]
