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


def email_exists_by_gmail_id(
    gmail_message_id: str,
    *,
    gmail_account_email: str | None = None,
) -> bool:
    """Return True if this Gmail message is already stored for the account."""
    if not gmail_message_id:
        return False

    client = get_supabase_client()

    query = (
        client.table("emails")
        .select("id")
        .eq("gmail_message_id", gmail_message_id)
    )
    if gmail_account_email:
        query = query.eq("gmail_account_email", gmail_account_email)

    response = query.limit(1).execute()
    return bool(response.data)


def get_email_by_gmail_id(
    gmail_message_id: str,
    *,
    gmail_account_email: str,
):
    client = get_supabase_client()
    response = (
        client.table("emails")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("gmail_message_id", gmail_message_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_email_record(email: dict, *, gmail_account_email: str) -> dict:
    """
    Persist a Gmail message without requiring AI analysis.

    Idempotent on (gmail_account_email, gmail_message_id).
    Does not reset analysis_status when the row already exists.
    """
    if not email.get("id"):
        raise ValueError("email id (gmail_message_id) is required")
    if not gmail_account_email:
        raise ValueError("gmail_account_email is required")

    client = get_supabase_client()
    existing = get_email_by_gmail_id(
        email["id"],
        gmail_account_email=gmail_account_email,
    )

    label_ids = email.get("label_ids") or []
    if not isinstance(label_ids, list):
        label_ids = list(label_ids)

    in_inbox = email.get("in_inbox")
    if in_inbox is None:
        in_inbox = "INBOX" in label_ids

    email_data = {
        "gmail_account_email": gmail_account_email,
        "gmail_message_id": email["id"],
        "gmail_thread_id": email.get("thread_id") or email.get("gmail_thread_id"),
        "subject": email.get("subject", ""),
        "sender": email.get("from") or email.get("sender", ""),
        "received_at": email.get("date") or email.get("received_at", ""),
        "snippet": email.get("snippet", ""),
        "body": email.get("body", ""),
        "internal_date": email.get("internal_date"),
        "label_ids": label_ids,
        "in_inbox": bool(in_inbox),
    }

    if existing:
        email_data["analysis_status"] = existing.get("analysis_status") or "pending"
    else:
        email_data["analysis_status"] = "pending"

    email_result = (
        client.table("emails")
        .upsert(
            email_data,
            on_conflict="gmail_account_email,gmail_message_id",
        )
        .execute()
    )

    if not email_result.data:
        raise RuntimeError("Failed to upsert email")

    return email_result.data[0]


def mark_email_not_in_inbox(
    gmail_message_id: str,
    *,
    gmail_account_email: str,
) -> None:
    client = get_supabase_client()
    existing = get_email_by_gmail_id(
        gmail_message_id,
        gmail_account_email=gmail_account_email,
    )
    if not existing:
        return

    labels = list(existing.get("label_ids") or [])
    labels = [lab for lab in labels if lab != "INBOX"]

    (
        client.table("emails")
        .update({"in_inbox": False, "label_ids": labels})
        .eq("id", existing["id"])
        .execute()
    )


def save_analysis_for_email(email_id: str, analysis: dict) -> None:
    client = get_supabase_client()

    analysis_data = {
        "email_id": email_id,
        "summary": analysis["summary"],
        "category": analysis["category"],
        "importance": analysis["importance"],
        "deadlines": analysis["deadlines"],
        "action_required": analysis["action_required"],
        "confidence": analysis["confidence"],
    }

    analysis_result = (
        client.table("email_analyses")
        .upsert(analysis_data, on_conflict="email_id")
        .execute()
    )

    if not analysis_result.data:
        raise RuntimeError("Failed to save email analysis")

    (
        client.table("emails")
        .update({"analysis_status": "done"})
        .eq("id", email_id)
        .execute()
    )


def mark_analysis_failed(email_id: str) -> None:
    client = get_supabase_client()
    (
        client.table("emails")
        .update({"analysis_status": "failed"})
        .eq("id", email_id)
        .execute()
    )


def list_pending_analysis_emails(
    *,
    gmail_account_email: str,
    limit: int = 20,
) -> list:
    client = get_supabase_client()
    response = (
        client.table("emails")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .in_("analysis_status", ["pending", "failed"])
        .order("internal_date", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def save_email_and_analysis(email: dict, analysis: dict) -> None:
    """
    Analyze path compatibility: upsert email then analysis.

    Prefer passing gmail_account_email on the email dict when available.
    """
    account = (
        email.get("gmail_account_email")
        or os.getenv("GMAIL_ACCOUNT_EMAIL")
        or "legacy"
    )
    row = upsert_email_record(email, gmail_account_email=account)
    save_analysis_for_email(row["id"], analysis)


def list_stored_emails(
    limit: int = 20,
    *,
    gmail_account_email: str | None = None,
    in_inbox_only: bool = True,
    cursor_internal_date: int | None = None,
):
    """List messages ordered by Gmail internal_date (not created_at)."""
    client = get_supabase_client()

    query = client.table("emails").select("*, email_analyses(*)")

    if gmail_account_email:
        query = query.eq("gmail_account_email", gmail_account_email)
    if in_inbox_only:
        query = query.eq("in_inbox", True)
    if cursor_internal_date is not None:
        query = query.lt("internal_date", cursor_internal_date)

    response = (
        query.order("internal_date", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


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


def search_emails_for_chat(
    *,
    gmail_account_email: str | None,
    sender_contains: str | None = None,
    keywords: list[str] | None = None,
    importance: str | None = None,
    action_required: bool | None = None,
    internal_date_from_ms: int | None = None,
    internal_date_to_ms: int | None = None,
    limit: int = 15,
    in_inbox_only: bool = True,
) -> list:
    """
    Filtered email retrieval for Ask My Inbox (PostgREST filters + in-memory refine).
    """
    client = get_supabase_client()
    fetch_limit = max(limit * 3, 40)

    query = client.table("emails").select("*, email_analyses(*)")
    if gmail_account_email:
        query = query.eq("gmail_account_email", gmail_account_email)
    if in_inbox_only:
        query = query.eq("in_inbox", True)
    if sender_contains:
        query = query.ilike("sender", f"%{sender_contains.strip()}%")
    if internal_date_from_ms is not None:
        query = query.gte("internal_date", internal_date_from_ms)
    if internal_date_to_ms is not None:
        query = query.lt("internal_date", internal_date_to_ms)

    rows = (
        query.order("internal_date", desc=True)
        .limit(fetch_limit)
        .execute()
        .data
        or []
    )

    keywords_norm = [k.strip().lower() for k in (keywords or []) if k and k.strip()]

    def _analysis(row: dict) -> dict:
        analyses = row.get("email_analyses") or []
        if isinstance(analyses, list):
            return analyses[0] if analyses else {}
        return analyses if isinstance(analyses, dict) else {}

    filtered: list = []
    for row in rows:
        analysis = _analysis(row)
        if importance and (analysis.get("importance") or "").lower() != importance.lower():
            continue
        if action_required is not None and bool(analysis.get("action_required")) != action_required:
            continue
        if keywords_norm:
            hay = " ".join(
                [
                    str(row.get("subject") or ""),
                    str(row.get("snippet") or ""),
                    str(row.get("body") or "")[:2000],
                    str(analysis.get("summary") or ""),
                ]
            ).lower()
            if not any(k in hay for k in keywords_norm):
                continue
        filtered.append(row)
        if len(filtered) >= limit:
            break

    return filtered


def _first_analysis(row: dict):
    analyses = row.get("email_analyses")
    if isinstance(analyses, list):
        return analyses[0] if analyses else None
    return analyses


def list_conversations(
    *,
    gmail_account_email: str,
    category: str = "primary",
    limit: int = 20,
    cursor_internal_date: int | None = None,
    cursor_thread_id: str | None = None,
    date_from_iso: str | None = None,
    date_to_iso: str | None = None,
) -> dict:
    """
    Conversation list matching Gmail-style newest-first threads.

    Groups by gmail_thread_id; orders by latest message internal_date DESC.
    Optional date_from_iso / date_to_iso (YYYY-MM-DD) filter on internal_date
    using BRIEFING_TIMEZONE day boundaries (inclusive both ends).
    """
    from briefing_service import date_iso_to_internal_ms_range
    from gmail_sync import message_matches_category

    client = get_supabase_client()
    fetch_limit = max(limit * 8, 80)
    start_ms, end_ms = date_iso_to_internal_ms_range(date_from_iso, date_to_iso)

    query = (
        client.table("emails")
        .select("*, email_analyses(*)")
        .eq("gmail_account_email", gmail_account_email)
        .eq("in_inbox", True)
    )
    if start_ms is not None:
        query = query.gte("internal_date", start_ms)
    if end_ms is not None:
        query = query.lt("internal_date", end_ms)
    if cursor_internal_date is not None:
        query = query.lte("internal_date", cursor_internal_date)

    rows = (
        query.order("internal_date", desc=True)
        .limit(fetch_limit)
        .execute()
        .data
        or []
    )

    conversations: list[dict] = []
    seen_threads: set[str] = set()
    skip_until_past_cursor = cursor_internal_date is not None

    for row in rows:
        labels = row.get("label_ids") or []
        if not message_matches_category(labels, category):
            continue

        thread_id = row.get("gmail_thread_id") or row.get("gmail_message_id")
        internal_date = row.get("internal_date") or 0

        if skip_until_past_cursor:
            if (
                internal_date == cursor_internal_date
                and thread_id == cursor_thread_id
            ):
                skip_until_past_cursor = False
            continue

        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)

        analysis = _first_analysis(row)
        conversations.append(
            {
                "thread_id": thread_id,
                "latest_email_id": row.get("id"),
                "gmail_message_id": row.get("gmail_message_id"),
                "subject": row.get("subject"),
                "sender": row.get("sender"),
                "snippet": row.get("snippet"),
                "internal_date": internal_date,
                "received_at": row.get("received_at"),
                "label_ids": labels,
                "analysis_status": row.get("analysis_status"),
                "email_analyses": analysis,
                "message_count": 1,
            }
        )
        if len(conversations) >= limit:
            break

    # Fill message_count for returned threads (best-effort).
    for conv in conversations:
        tid = conv["thread_id"]
        try:
            count_resp = (
                client.table("emails")
                .select("id")
                .eq("gmail_account_email", gmail_account_email)
                .eq("gmail_thread_id", tid)
                .eq("in_inbox", True)
                .execute()
            )
            conv["message_count"] = len(count_resp.data or [])
        except Exception:
            conv["message_count"] = 1

    next_cursor = None
    if len(conversations) >= limit:
        last = conversations[-1]
        next_cursor = {
            "internal_date": last["internal_date"],
            "thread_id": last["thread_id"],
        }

    return {
        "conversations": conversations,
        "next_cursor": next_cursor,
        "category": category,
    }


def list_thread_messages(
    *,
    gmail_account_email: str,
    thread_id: str,
) -> list:
    client = get_supabase_client()
    response = (
        client.table("emails")
        .select("*, email_analyses(*)")
        .eq("gmail_account_email", gmail_account_email)
        .eq("gmail_thread_id", thread_id)
        .order("internal_date", desc=False)
        .execute()
    )
    return response.data or []


def get_sync_state(gmail_account_email: str) -> dict | None:
    client = get_supabase_client()
    response = (
        client.table("gmail_sync_state")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_sync_state(
    gmail_account_email: str,
    *,
    history_id: str | None = None,
    backfill_page_token: str | None = None,
    backfill_status: str | None = None,
    clear_page_token: bool = False,
) -> dict:
    client = get_supabase_client()
    existing = get_sync_state(gmail_account_email) or {}

    row = {
        "gmail_account_email": gmail_account_email,
        "history_id": (
            history_id if history_id is not None else existing.get("history_id")
        ),
        "backfill_status": (
            backfill_status
            if backfill_status is not None
            else existing.get("backfill_status") or "pending"
        ),
        "updated_at": _utcnow(),
    }

    if clear_page_token:
        row["backfill_page_token"] = None
    elif backfill_page_token is not None:
        row["backfill_page_token"] = backfill_page_token
    else:
        row["backfill_page_token"] = existing.get("backfill_page_token")

    result = (
        client.table("gmail_sync_state")
        .upsert(row, on_conflict="gmail_account_email")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert gmail_sync_state")
    return result.data[0]


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


def list_emails_for_briefing_day(
    *,
    gmail_account_email: str,
    start_ms: int,
    end_ms: int,
    limit: int = 30,
) -> list:
    """Emails whose Gmail internal_date falls in [start_ms, end_ms)."""
    if not gmail_account_email:
        return []

    client = get_supabase_client()
    response = (
        client.table("emails")
        .select("*, email_analyses(*)")
        .eq("gmail_account_email", gmail_account_email)
        .gte("internal_date", start_ms)
        .lt("internal_date", end_ms)
        .order("internal_date", desc=True)
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
    gmail_account_email: str,
    briefing_date: str,
) -> dict:
    if not gmail_account_email:
        raise ValueError("gmail_account_email is required to save a briefing")
    if not briefing_date:
        raise ValueError("briefing_date is required to save a briefing")

    client = get_supabase_client()

    row = {
        "gmail_account_email": gmail_account_email,
        "briefing_date": briefing_date,
        "content": content,
        "period_start": period_start,
        "period_end": period_end,
        "email_count": email_count,
    }

    result = client.table("daily_briefings").insert(row).execute()

    if not result.data:
        raise RuntimeError("Failed to save daily briefing")

    return result.data[0]


def get_latest_briefing(gmail_account_email: str | None = None):
    client = get_supabase_client()

    query = client.table("daily_briefings").select("*")
    if gmail_account_email:
        query = query.eq("gmail_account_email", gmail_account_email)

    response = (
        query.order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def list_briefings_since(
    *,
    gmail_account_email: str,
    since_iso: str,
    since_briefing_date: str | None = None,
) -> list:
    """
    Briefings for one account in the history window.

    Prefer filtering by briefing_date when available; also include rows
    with created_at >= since_iso for legacy compatibility.
    """
    if not gmail_account_email:
        return []

    client = get_supabase_client()
    query = (
        client.table("daily_briefings")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
    )
    if since_briefing_date:
        query = query.gte("briefing_date", since_briefing_date)
    else:
        query = query.gte("created_at", since_iso)

    response = query.order("created_at", desc=True).execute()
    return response.data or []