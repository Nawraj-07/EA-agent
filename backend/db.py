import logging
import os
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

from egress_meter import enabled as egress_meter_enabled
from egress_meter import payload_bytes, record_supabase, using_operation


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env", override=False)

# List and ranking reads. Full body stays on the detail, thread, draft, and
# analysis paths.
EMAIL_LIST_COLUMNS = (
    "id,gmail_account_email,gmail_message_id,gmail_thread_id,"
    "subject,sender,snippet,received_at,internal_date,label_ids,"
    "in_inbox,analysis_status"
)
ANALYSIS_LIST_COLUMNS = (
    "summary,category,importance,deadlines,action_required,confidence"
)
EMAIL_LIST_WITH_ANALYSIS = (
    f"{EMAIL_LIST_COLUMNS},email_analyses({ANALYSIS_LIST_COLUMNS})"
)
EMAIL_DETAIL_WITH_ANALYSIS = (
    f"{EMAIL_LIST_COLUMNS},body,email_analyses({ANALYSIS_LIST_COLUMNS})"
)
EMAIL_SYNC_LOOKUP_COLUMNS = "id,analysis_status,label_ids,gmail_message_id"

_supabase_client = None
_CONV_CACHE: dict[tuple, tuple[float, dict]] = {}
_CONV_CACHE_TTL_SECONDS = 15.0


class _MeteredQuery:
    """Count Supabase response bytes without keeping the payload."""

    def __init__(self, inner, table: str):
        self._inner = inner
        self._table = table

    def execute(self, *args, **kwargs):
        result = self._inner.execute(*args, **kwargs)
        data = getattr(result, "data", None)
        if isinstance(data, list):
            rows = len(data)
        elif data:
            rows = 1
        else:
            rows = 0
        record_supabase(self._table, rows, payload_bytes(data))
        return result

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            out = attr(*args, **kwargs)
            if out is self._inner:
                return self
            if hasattr(out, "execute"):
                return _MeteredQuery(out, self._table)
            return out

        return wrapped


class _MeteredClient:
    def __init__(self, inner):
        self._inner = inner

    def table(self, name: str):
        return _MeteredQuery(self._inner.table(name), name)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def get_supabase_client():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    if not url or not service_role_key:
        raise RuntimeError("Supabase credentials are missing")

    client = create_client(url, service_role_key)
    if egress_meter_enabled():
        client = _MeteredClient(client)
    _supabase_client = client
    return _supabase_client


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


def existing_gmail_message_ids(
    gmail_message_ids: list[str],
    *,
    gmail_account_email: str,
) -> set[str]:
    """Return which of the given Gmail ids already exist locally for the account."""
    ids = [mid for mid in gmail_message_ids if mid]
    if not ids or not gmail_account_email:
        return set()

    client = get_supabase_client()
    found: set[str] = set()
    chunk_size = 100
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        response = (
            client.table("emails")
            .select("gmail_message_id")
            .eq("gmail_account_email", gmail_account_email)
            .in_("gmail_message_id", chunk)
            .execute()
        )
        for row in response.data or []:
            mid = row.get("gmail_message_id")
            if mid:
                found.add(str(mid))
    return found


def get_email_by_gmail_id(
    gmail_message_id: str,
    *,
    gmail_account_email: str,
):
    client = get_supabase_client()
    response = (
        client.table("emails")
        .select(EMAIL_SYNC_LOOKUP_COLUMNS)
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
        .select("id,analysis_status,gmail_message_id")
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


def auto_analyze_cutoff_ms(now: datetime | None = None) -> int | None:
    """Oldest internal_date the analysis queue will pick (None = no cutoff)."""
    raw = os.getenv("GMAIL_AUTO_ANALYZE_NEWER_THAN_DAYS", "2")
    try:
        days = int(raw)
    except ValueError:
        days = 2
    if days <= 0:
        return None
    stamp = now or datetime.now(timezone.utc)
    cutoff = stamp - timedelta(days=days)
    return int(cutoff.timestamp() * 1000)


def list_pending_analysis_emails(
    *,
    gmail_account_email: str,
    limit: int = 20,
    statuses: list[str] | None = None,
    min_internal_date_ms: int | None | object = ...,
) -> list:
    client = get_supabase_client()
    allowed = statuses or ["pending", "failed"]
    cutoff = (
        auto_analyze_cutoff_ms()
        if min_internal_date_ms is ...
        else min_internal_date_ms
    )
    query = (
        client.table("emails")
        .select(EMAIL_DETAIL_WITH_ANALYSIS)
        .eq("gmail_account_email", gmail_account_email)
        .in_("analysis_status", allowed)
    )
    if cutoff is not None:
        query = query.gte("internal_date", cutoff)
    response = (
        query.order("internal_date", desc=True)
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
    include_body: bool = False,
):
    """List messages ordered by Gmail internal_date (not created_at)."""
    client = get_supabase_client()
    columns = EMAIL_DETAIL_WITH_ANALYSIS if include_body else EMAIL_LIST_WITH_ANALYSIS

    query = client.table("emails").select(columns)

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
        .select(EMAIL_DETAIL_WITH_ANALYSIS)
        .eq("id", email_id)
        .maybe_single()
        .execute()
    )

    return response.data


def hydrate_email_bodies(emails: list[dict]) -> list[dict]:
    """Load stored bodies for rows that do not already have one."""
    missing = [
        str(email.get("id"))
        for email in emails
        if email.get("id") and "body" not in email
    ]
    missing = [
        eid
        for eid in missing
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            eid,
        )
    ]
    if not missing:
        return emails
    client = get_supabase_client()
    found: dict[str, str] = {}
    for start in range(0, len(missing), 50):
        chunk = missing[start : start + 50]
        response = (
            client.table("emails")
            .select("id,body")
            .in_("id", chunk)
            .execute()
        )
        for row in response.data or []:
            found[str(row.get("id"))] = row.get("body") or ""
    for email in emails:
        eid = str(email.get("id") or "")
        if eid in found:
            email["body"] = found[eid]
    return emails


_ILIKE_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def tokenize_search_keywords(keywords: list[str] | None) -> list[str]:
    """Split phrases into tokens and drop short stopwords (not an exact phrase)."""
    stopwords = {
        "a",
        "an",
        "and",
        "the",
        "of",
        "or",
        "to",
        "in",
        "on",
        "for",
        "from",
        "with",
        "about",
        "this",
        "that",
        "did",
        "how",
        "many",
        "what",
        "which",
        "who",
        "when",
        "where",
        "email",
        "emails",
        "mail",
        "mails",
        "inbox",
        "please",
        "me",
        "my",
        "i",
    }
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in keywords or []:
        for part in _ILIKE_SAFE.split(str(raw).lower()):
            token = part.strip("-_")
            if len(token) < 2 or token in stopwords or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


def _escape_ilike_token(token: str) -> str:
    return token.replace("\\", "").replace("%", "").replace("_", "").replace(",", "")


def row_matches_all_tokens(row: dict, tokens: list[str]) -> bool:
    if not tokens:
        return True
    analysis = _first_analysis(row) or {}
    hay = " ".join(
        [
            str(row.get("subject") or ""),
            str(row.get("snippet") or ""),
            str(row.get("body") or ""),
            str(row.get("sender") or ""),
            str(analysis.get("summary") or ""),
        ]
    ).lower()
    return all(token in hay for token in tokens)


def completed_analysis_payload(email: dict | None) -> dict | None:
    """Return a saved analysis when status is done; otherwise None (no OpenAI)."""
    if not email:
        return None
    if str(email.get("analysis_status") or "") != "done":
        return None
    analysis = _first_analysis(email)
    if not isinstance(analysis, dict) or not analysis.get("summary"):
        return None
    return analysis


def _apply_chat_row_filters(
    query,
    *,
    gmail_account_email: str | None,
    sender_contains: str | None,
    tokens: list[str],
    internal_date_from_ms: int | None,
    internal_date_to_ms: int | None,
    in_inbox_only: bool,
    importance: str | None = None,
    action_required: bool | None = None,
    unread_only: bool = False,
    label_contains: str | None = None,
):
    if gmail_account_email:
        query = query.eq("gmail_account_email", gmail_account_email)
    if in_inbox_only:
        query = query.eq("in_inbox", True)
    if label_contains and str(label_contains).strip():
        query = query.contains("label_ids", [str(label_contains).strip()])
    if unread_only:
        query = query.contains("label_ids", ["UNREAD"])
    if sender_contains and sender_contains.strip():
        query = query.ilike("sender", f"%{sender_contains.strip()}%")
    if internal_date_from_ms is not None:
        query = query.gte("internal_date", internal_date_from_ms)
    if internal_date_to_ms is not None:
        query = query.lt("internal_date", internal_date_to_ms)
    if importance:
        query = query.eq("email_analyses.importance", importance)
    if action_required is not None:
        query = query.eq("email_analyses.action_required", action_required)
    for token in tokens:
        safe = _escape_ilike_token(token)
        if not safe:
            continue
        query = query.or_(
            f"subject.ilike.%{safe}%,"
            f"snippet.ilike.%{safe}%,"
            f"body.ilike.%{safe}%"
        )
    return query


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
    unread_only: bool = False,
    label_contains: str | None = None,
) -> list:
    """
    Filtered email retrieval for Ask My Inbox (PostgREST filters + in-memory refine).
    """
    with using_operation("chat_search"):
        return _search_emails_for_chat(
            gmail_account_email=gmail_account_email,
            sender_contains=sender_contains,
            keywords=keywords,
            importance=importance,
            action_required=action_required,
            internal_date_from_ms=internal_date_from_ms,
            internal_date_to_ms=internal_date_to_ms,
            limit=limit,
            in_inbox_only=in_inbox_only,
            unread_only=unread_only,
            label_contains=label_contains,
        )


def _search_emails_for_chat(
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
    unread_only: bool = False,
    label_contains: str | None = None,
) -> list:
    client = get_supabase_client()
    tokens = tokenize_search_keywords(keywords)
    needs_analysis_filter = importance is not None or action_required is not None
    # The caller passes the pool size. SQL already applies keyword filters,
    # so this read does not multiply the limit again.
    fetch_limit = min(max(int(limit or 1), 1), 120)
    select = (
        f"{EMAIL_LIST_COLUMNS},email_analyses!inner({ANALYSIS_LIST_COLUMNS})"
        if needs_analysis_filter
        else EMAIL_LIST_WITH_ANALYSIS
    )

    query = client.table("emails").select(select)
    query = _apply_chat_row_filters(
        query,
        gmail_account_email=gmail_account_email,
        sender_contains=sender_contains,
        tokens=tokens,
        internal_date_from_ms=internal_date_from_ms,
        internal_date_to_ms=internal_date_to_ms,
        in_inbox_only=in_inbox_only,
        importance=importance,
        action_required=action_required,
        unread_only=unread_only,
        label_contains=label_contains,
    )

    rows = (
        query.order("internal_date", desc=True)
        .limit(fetch_limit)
        .execute()
        .data
        or []
    )

    filtered: list = []
    for row in rows:
        analysis = _first_analysis(row) or {}
        if importance and (analysis.get("importance") or "").lower() != importance.lower():
            continue
        if action_required is not None and bool(analysis.get("action_required")) != action_required:
            continue
        if "body" in row and not row_matches_all_tokens(row, tokens):
            continue
        filtered.append(row)
        if len(filtered) >= limit:
            break

    return filtered


def count_emails_for_chat(
    *,
    gmail_account_email: str | None,
    sender_contains: str | None = None,
    keywords: list[str] | None = None,
    importance: str | None = None,
    action_required: bool | None = None,
    internal_date_from_ms: int | None = None,
    internal_date_to_ms: int | None = None,
    in_inbox_only: bool = True,
    unread_only: bool = False,
    label_contains: str | None = None,
) -> int:
    """Postgres COUNT of stored rows matching the same chat filters."""
    client = get_supabase_client()
    tokens = tokenize_search_keywords(keywords)
    needs_analysis_filter = importance is not None or action_required is not None
    select = "id, email_analyses!inner(importance, action_required)" if needs_analysis_filter else "id"
    query = client.table("emails").select(select, count="exact")
    query = _apply_chat_row_filters(
        query,
        gmail_account_email=gmail_account_email,
        sender_contains=sender_contains,
        tokens=tokens,
        internal_date_from_ms=internal_date_from_ms,
        internal_date_to_ms=internal_date_to_ms,
        in_inbox_only=in_inbox_only,
        importance=importance,
        action_required=action_required,
        unread_only=unread_only,
        label_contains=label_contains,
    )
    response = query.limit(0).execute()
    counted = getattr(response, "count", None)
    if counted is not None:
        return int(counted)
    return len(response.data or [])


def _first_analysis(row: dict | None):
    if not row:
        return None
    analyses = row.get("email_analyses")
    if isinstance(analyses, list):
        return analyses[0] if analyses else None
    return analyses


def list_conversations(
    *,
    gmail_account_email: str,
    category: str = "inbox",
    limit: int = 20,
    cursor_internal_date: int | None = None,
    cursor_thread_id: str | None = None,
    date_from_iso: str | None = None,
    date_to_iso: str | None = None,
) -> dict:
    """Conversation list. See _list_conversations_impl."""
    with using_operation("inbox_list"):
        return _list_conversations_impl(
            gmail_account_email=gmail_account_email,
            category=category,
            limit=limit,
            cursor_internal_date=cursor_internal_date,
            cursor_thread_id=cursor_thread_id,
            date_from_iso=date_from_iso,
            date_to_iso=date_to_iso,
        )


def _list_conversations_impl(
    *,
    gmail_account_email: str,
    category: str = "inbox",
    limit: int = 20,
    cursor_internal_date: int | None = None,
    cursor_thread_id: str | None = None,
    date_from_iso: str | None = None,
    date_to_iso: str | None = None,
) -> dict:
    """
    Conversation list matching Gmail-style newest-first threads.

    Default category=inbox (every INBOX row). Other tabs filter Gmail labels.
    Groups by gmail_thread_id; orders by latest message internal_date DESC.
    Optional date_from_iso / date_to_iso (YYYY-MM-DD) filter on internal_date
    using BRIEFING_TIMEZONE day boundaries (inclusive both ends).
    """
    from briefing_service import date_iso_to_internal_ms_range
    from gmail_sync import message_matches_category

    cache_key = (
        gmail_account_email,
        category,
        limit,
        cursor_internal_date,
        cursor_thread_id,
        date_from_iso,
        date_to_iso,
    )
    cached = _CONV_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < _CONV_CACHE_TTL_SECONDS:
        stored = cached[1]
        cursor = stored.get("next_cursor")
        return {
            "conversations": [dict(item) for item in stored.get("conversations") or []],
            "next_cursor": dict(cursor) if isinstance(cursor, dict) else None,
            "category": stored.get("category"),
        }

    client = get_supabase_client()
    scan_batch = max(limit * 2, 40)
    max_scan_rows = 800
    start_ms, end_ms = date_iso_to_internal_ms_range(date_from_iso, date_to_iso)

    conversations: list[dict] = []
    seen_threads: set[str] = set()
    skip_until_past_cursor = cursor_internal_date is not None
    offset = 0
    scanned = 0

    while len(conversations) < limit and scanned < max_scan_rows:
        query = (
            client.table("emails")
            .select(EMAIL_LIST_WITH_ANALYSIS)
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
            .range(offset, offset + scan_batch - 1)
            .execute()
            .data
            or []
        )
        if not rows:
            break

        offset += len(rows)
        scanned += len(rows)

        for row in rows:
            labels = row.get("label_ids") or []
            thread_id = row.get("gmail_thread_id") or row.get("gmail_message_id")
            internal_date = row.get("internal_date") or 0

            if skip_until_past_cursor:
                if (
                    internal_date == cursor_internal_date
                    and thread_id == cursor_thread_id
                ):
                    skip_until_past_cursor = False
                continue

            if not message_matches_category(labels, category):
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

        if len(rows) < scan_batch:
            break

    thread_ids = [conv["thread_id"] for conv in conversations if conv.get("thread_id")]
    tallies: dict[str, int] = {}
    if thread_ids:
        try:
            count_resp = (
                client.table("emails")
                .select("gmail_thread_id")
                .eq("gmail_account_email", gmail_account_email)
                .eq("in_inbox", True)
                .in_("gmail_thread_id", thread_ids)
                .execute()
            )
            for row in count_resp.data or []:
                tid = row.get("gmail_thread_id")
                if tid:
                    tallies[tid] = tallies.get(tid, 0) + 1
        except Exception:
            logger.exception("Conversation message counts failed")
    for conv in conversations:
        conv["message_count"] = tallies.get(conv["thread_id"], 1)

    next_cursor = None
    if len(conversations) >= limit:
        last = conversations[-1]
        next_cursor = {
            "internal_date": last["internal_date"],
            "thread_id": last["thread_id"],
        }

    result = {
        "conversations": conversations,
        "next_cursor": next_cursor,
        "category": category,
    }
    _CONV_CACHE[cache_key] = (now, result)
    if len(_CONV_CACHE) > 32:
        oldest = min(_CONV_CACHE, key=lambda key: _CONV_CACHE[key][0])
        _CONV_CACHE.pop(oldest, None)
    return result


def list_thread_messages(
    *,
    gmail_account_email: str,
    thread_id: str,
    include_body: bool = True,
) -> list:
    client = get_supabase_client()
    columns = EMAIL_DETAIL_WITH_ANALYSIS if include_body else EMAIL_LIST_WITH_ANALYSIS
    response = (
        client.table("emails")
        .select(columns)
        .eq("gmail_account_email", gmail_account_email)
        .eq("gmail_thread_id", thread_id)
        .order("internal_date", desc=False)
        .execute()
    )
    return response.data or []


def list_followup_threads(
    *,
    gmail_account_email: str,
    thread_ids: list[str],
) -> dict[str, list]:
    """Metadata for many threads in one read. Bodies stay out of this scan."""
    grouped: dict[str, list] = {}
    ids = [tid for tid in thread_ids if tid and not str(tid).startswith("msg:")]
    if not ids or not gmail_account_email:
        return grouped
    client = get_supabase_client()
    for start in range(0, len(ids), 50):
        chunk = ids[start : start + 50]
        response = (
            client.table("emails")
            .select(EMAIL_LIST_WITH_ANALYSIS)
            .eq("gmail_account_email", gmail_account_email)
            .in_("gmail_thread_id", chunk)
            .execute()
        )
        for row in response.data or []:
            tid = str(row.get("gmail_thread_id") or "")
            if tid:
                grouped.setdefault(tid, []).append(row)
    return grouped


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


def get_gmail_oauth_row(gmail_account_email: str) -> dict | None:
    """Return the encrypted OAuth row for this mailbox, or None.

    Callers must never log ciphertext or decrypted JSON.
    """
    account = (gmail_account_email or "").strip().lower()
    if not account:
        return None

    client = get_supabase_client()
    response = (
        client.table("gmail_oauth_credentials")
        .select(
            "gmail_account_email,ciphertext,key_version,token_expiry,updated_at"
        )
        .eq("gmail_account_email", account)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_gmail_oauth_row(
    gmail_account_email: str,
    *,
    ciphertext: str,
    key_version: int = 1,
    token_expiry: str | None = None,
) -> dict:
    """Insert or replace the encrypted OAuth blob. Never log ciphertext."""
    account = (gmail_account_email or "").strip().lower()
    if not account:
        raise ValueError("gmail_account_email is required")
    if not ciphertext or not str(ciphertext).strip():
        raise ValueError("ciphertext is required")

    client = get_supabase_client()
    row = {
        "gmail_account_email": account,
        "ciphertext": str(ciphertext).strip(),
        "key_version": int(key_version),
        "token_expiry": token_expiry,
        "updated_at": _utcnow(),
    }
    result = (
        client.table("gmail_oauth_credentials")
        .upsert(row, on_conflict="gmail_account_email")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert gmail_oauth_credentials")
    return {
        "gmail_account_email": result.data[0].get("gmail_account_email"),
        "key_version": result.data[0].get("key_version"),
        "token_expiry": result.data[0].get("token_expiry"),
        "updated_at": result.data[0].get("updated_at"),
    }


def _count_account_rows(table: str, gmail_account_email: str) -> int:
    client = get_supabase_client()
    result = (
        client.table(table)
        .select("id", count="exact")
        .eq("gmail_account_email", gmail_account_email)
        .limit(1)
        .execute()
    )
    return int(result.count or 0)


def inspect_account_row_counts() -> list[dict]:
    """Counts per gmail_account_email. Does not delete anything."""
    client = get_supabase_client()
    accounts: set[str] = set()
    for table in ("gmail_sync_state", "daily_briefings"):
        rows = (
            client.table(table)
            .select("gmail_account_email")
            .execute()
            .data
            or []
        )
        for row in rows:
            email = (row.get("gmail_account_email") or "").strip().lower()
            if email:
                accounts.add(email)
    return [
        {
            "gmail_account_email": account,
            "emails": _count_account_rows("emails", account),
            "daily_briefings": _count_account_rows("daily_briefings", account),
            "gmail_sync_state": _count_account_rows("gmail_sync_state", account),
        }
        for account in sorted(accounts)
    ]


def purge_local_account_copies(gmail_account_email: str) -> dict:
    """
    Remove leftover *local* copies for a disconnected mailbox.

    Never deletes Gmail. Never deletes gaurang@rbhargavaassociates.in rows.
    """
    from gmail_safety import assert_can_purge_local_account

    account = assert_can_purge_local_account(gmail_account_email)
    before = {
        "emails": _count_account_rows("emails", account),
        "daily_briefings": _count_account_rows("daily_briefings", account),
        "gmail_sync_state": _count_account_rows("gmail_sync_state", account),
    }

    client = get_supabase_client()
    client.table("emails").delete().eq("gmail_account_email", account).execute()
    client.table("daily_briefings").delete().eq(
        "gmail_account_email", account
    ).execute()
    client.table("gmail_sync_state").delete().eq(
        "gmail_account_email", account
    ).execute()

    after = {
        "emails": _count_account_rows("emails", account),
        "daily_briefings": _count_account_rows("daily_briefings", account),
        "gmail_sync_state": _count_account_rows("gmail_sync_state", account),
    }
    return {"account": account, "before": before, "after": after}


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


def list_emails_for_briefing(
    since_iso: str,
    limit: int = 30,
    *,
    include_body: bool = False,
) -> list:
    """Recent stored emails (with analyses) for the daily briefing window."""
    client = get_supabase_client()
    columns = EMAIL_DETAIL_WITH_ANALYSIS if include_body else EMAIL_LIST_WITH_ANALYSIS

    response = (
        client.table("emails")
        .select(columns)
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
    include_body: bool = False,
) -> list:
    """Emails whose Gmail internal_date falls in [start_ms, end_ms)."""
    if not gmail_account_email:
        return []

    client = get_supabase_client()
    columns = EMAIL_DETAIL_WITH_ANALYSIS if include_body else EMAIL_LIST_WITH_ANALYSIS
    response = (
        client.table("emails")
        .select(columns)
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


def get_briefing_for_date(
    *,
    gmail_account_email: str,
    briefing_date: str,
) -> dict | None:
    """Latest saved briefing row for one calendar day (briefing_date)."""
    if not gmail_account_email or not briefing_date:
        return None

    client = get_supabase_client()
    response = (
        client.table("daily_briefings")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("briefing_date", briefing_date)
        .order("created_at", desc=True)
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

def claim_whatsapp_outbox_row(
    *,
    notification_type: str,
    recipient: str,
    source_id: str,
    template_name: str,
    template_language: str = "en_US",
    payload_summary: dict | None = None,
) -> dict | None:
    """Insert a queued outbox row. Returns None if the unique key already exists."""
    recipient_clean = (recipient or "").strip()
    source = (source_id or "").strip()
    if not recipient_clean or not source or not notification_type or not template_name:
        raise ValueError("whatsapp outbox requires type, recipient, source_id, template")

    client = get_supabase_client()
    existing = (
        client.table("whatsapp_notification_outbox")
        .select("id,status,meta_message_id,retry_count")
        .eq("notification_type", notification_type)
        .eq("source_id", source)
        .eq("recipient", recipient_clean)
        .limit(1)
        .execute()
    )
    if existing.data:
        return None

    row = {
        "notification_type": notification_type,
        "recipient": recipient_clean,
        "source_id": source,
        "template_name": template_name,
        "template_language": template_language or "en_US",
        "status": "queued",
        "payload_summary": payload_summary or {},
        "retry_count": 0,
        "created_at": _utcnow(),
    }
    result = (
        client.table("whatsapp_notification_outbox")
        .insert(row)
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to insert whatsapp_notification_outbox row")
    return result.data[0]


def update_whatsapp_outbox_status(
    outbox_id: str,
    *,
    status: str,
    meta_message_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    increment_retry: bool = False,
) -> dict | None:
    if not outbox_id:
        return None
    client = get_supabase_client()
    updates: dict = {
        "status": status,
        "error_code": error_code,
        "error_message": (error_message or None) and str(error_message)[:500],
    }
    if meta_message_id:
        updates["meta_message_id"] = meta_message_id
    if status in {"accepted", "sent"}:
        updates["sent_at"] = _utcnow()
    if status == "delivered":
        updates["delivered_at"] = _utcnow()

    if increment_retry:
        current = (
            client.table("whatsapp_notification_outbox")
            .select("retry_count")
            .eq("id", outbox_id)
            .limit(1)
            .execute()
        )
        prior = 0
        if current.data:
            prior = int(current.data[0].get("retry_count") or 0)
        updates["retry_count"] = prior + 1

    result = (
        client.table("whatsapp_notification_outbox")
        .update(updates)
        .eq("id", outbox_id)
        .select("*")
        .execute()
    )
    return result.data[0] if result.data else None


def list_whatsapp_outbox_for_flush(
    *,
    statuses: list[str] | None = None,
    limit: int = 20,
    max_retries: int = 3,
) -> list[dict]:
    client = get_supabase_client()
    wanted = statuses or ["queued", "failed", "rate_limited"]
    response = (
        client.table("whatsapp_notification_outbox")
        .select("*")
        .in_("status", wanted)
        .lt("retry_count", max_retries)
        .order("created_at", desc=False)
        .limit(max(1, min(limit, 100)))
        .execute()
    )
    return response.data or []


def list_emails_for_briefing_range(
    *,
    gmail_account_email: str,
    start_ms: int,
    end_ms: int,
    limit: int = 120,
    include_body: bool = False,
) -> list:
    """Emails whose Gmail internal_date falls in [start_ms, end_ms)."""
    if not gmail_account_email:
        return []
    client = get_supabase_client()
    columns = EMAIL_DETAIL_WITH_ANALYSIS if include_body else EMAIL_LIST_WITH_ANALYSIS
    response = (
        client.table("emails")
        .select(columns)
        .eq("gmail_account_email", gmail_account_email)
        .gte("internal_date", start_ms)
        .lt("internal_date", end_ms)
        .order("internal_date", desc=True)
        .limit(max(1, min(int(limit), 300)))
        .execute()
    )
    return response.data or []


def upsert_smart_briefing_mode_run(
    *,
    gmail_account_email: str,
    mode: str,
    period_key: str,
    period_start: str,
    period_end: str,
    content: dict,
    email_count: int,
    status: str = "saved",
    error_message: str | None = None,
) -> dict:
    """Insert or update one mode run (unique on account+mode+period_key)."""
    if not gmail_account_email or not mode or not period_key:
        raise ValueError("account, mode, and period_key are required")

    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "gmail_account_email": gmail_account_email,
        "mode": mode,
        "period_key": period_key,
        "period_start": period_start,
        "period_end": period_end,
        "content": content or {},
        "email_count": int(email_count or 0),
        "status": status,
        "error_message": error_message,
        "updated_at": now,
    }
    result = (
        client.table("smart_briefing_mode_runs")
        .upsert(row, on_conflict="gmail_account_email,mode,period_key")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert smart_briefing_mode_runs row")
    return result.data[0]


def get_latest_smart_briefing_mode_run(
    *,
    gmail_account_email: str,
    mode: str,
) -> dict | None:
    if not gmail_account_email or not mode:
        return None
    client = get_supabase_client()
    response = (
        client.table("smart_briefing_mode_runs")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("mode", mode)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def list_smart_briefing_mode_runs(
    *,
    gmail_account_email: str,
    mode: str,
    limit: int = 14,
) -> list:
    if not gmail_account_email or not mode:
        return []
    client = get_supabase_client()
    response = (
        client.table("smart_briefing_mode_runs")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("mode", mode)
        .order("created_at", desc=True)
        .limit(max(1, min(int(limit), 60)))
        .execute()
    )
    return response.data or []


def list_recommendation_states_for_account(
    *,
    gmail_account_email: str,
) -> list:
    """All recommendation state rows for one Gmail account."""
    if not gmail_account_email:
        return []
    client = get_supabase_client()
    response = (
        client.table("briefing_recommendation_state")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .order("updated_at", desc=True)
        .execute()
    )
    return response.data or []


def get_recommendation_state(
    *,
    gmail_account_email: str,
    recommendation_key: str,
) -> dict | None:
    if not gmail_account_email or not recommendation_key:
        return None
    client = get_supabase_client()
    response = (
        client.table("briefing_recommendation_state")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("recommendation_key", recommendation_key)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_recommendation_state(
    *,
    gmail_account_email: str,
    source_type: str,
    source_id: str,
    recommendation_key: str,
    status: str,
    snoozed_until: str | None = None,
    title: str | None = None,
    action_type: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Insert or update one recommendation state row (account + key unique)."""
    if not gmail_account_email or not recommendation_key:
        raise ValueError("account and recommendation_key are required")

    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "gmail_account_email": gmail_account_email,
        "source_type": source_type or "briefing_item",
        "source_id": source_id or recommendation_key,
        "recommendation_key": recommendation_key,
        "status": status,
        "snoozed_until": snoozed_until,
        "title": title,
        "action_type": action_type,
        "metadata": metadata or {},
        "updated_at": now,
    }
    result = (
        client.table("briefing_recommendation_state")
        .upsert(row, on_conflict="gmail_account_email,recommendation_key")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert briefing_recommendation_state row")
    return result.data[0]


def release_expired_recommendation_snoozes(
    *,
    gmail_account_email: str,
    now_iso: str,
) -> int:
    """
    Move expired snoozed rows back to pending for one account.
    Returns number of rows updated (best-effort; 0 on empty).
    """
    if not gmail_account_email:
        return 0
    client = get_supabase_client()
    existing = (
        client.table("briefing_recommendation_state")
        .select("id,recommendation_key,snoozed_until")
        .eq("gmail_account_email", gmail_account_email)
        .eq("status", "snoozed")
        .lte("snoozed_until", now_iso)
        .execute()
    )
    rows = existing.data or []
    if not rows:
        return 0
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        client.table("briefing_recommendation_state").update(
            {
                "status": "pending",
                "snoozed_until": None,
                "updated_at": now,
            }
        ).eq("id", row["id"]).eq(
            "gmail_account_email", gmail_account_email
        ).execute()
        updated += 1
    return updated


def get_briefing_preferences(*, gmail_account_email: str) -> dict | None:
    if not gmail_account_email:
        return None
    client = get_supabase_client()
    response = (
        client.table("briefing_preferences")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_briefing_preferences(
    *,
    gmail_account_email: str,
    preferences: dict,
) -> dict:
    if not gmail_account_email:
        raise ValueError("gmail_account_email is required")
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "gmail_account_email": gmail_account_email,
        "timezone": preferences.get("timezone") or "Asia/Kolkata",
        "working_days": preferences.get("working_days") or [1, 2, 3, 4, 5],
        "working_hours": preferences.get("working_hours")
        or {"start": "09:00", "end": "18:00"},
        "briefing_sections": preferences.get("briefing_sections") or {},
        "vip_contacts": preferences.get("vip_contacts") or [],
        "filtering_preferences": preferences.get("filtering_preferences") or {},
        "mode_schedules": preferences.get("mode_schedules") or {},
        "updated_at": now,
    }
    result = (
        client.table("briefing_preferences")
        .upsert(row, on_conflict="gmail_account_email")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert briefing_preferences")
    return result.data[0]


def upsert_recommendation_feedback(
    *,
    gmail_account_email: str,
    recommendation_key: str,
    feedback_type: str,
    metadata: dict | None = None,
) -> dict:
    if not gmail_account_email or not recommendation_key:
        raise ValueError("account and recommendation_key are required")
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "gmail_account_email": gmail_account_email,
        "recommendation_key": recommendation_key,
        "feedback_type": feedback_type,
        "metadata": metadata or {},
        "updated_at": now,
    }
    result = (
        client.table("briefing_recommendation_feedback")
        .upsert(row, on_conflict="gmail_account_email,recommendation_key")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert briefing_recommendation_feedback")
    return result.data[0]


def list_recommendation_feedback_for_account(
    *,
    gmail_account_email: str,
) -> list:
    if not gmail_account_email:
        return []
    client = get_supabase_client()
    response = (
        client.table("briefing_recommendation_feedback")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .order("updated_at", desc=True)
        .execute()
    )
    return response.data or []


def try_claim_briefing_schedule(
    *,
    gmail_account_email: str,
    job_kind: str,
    period_key: str,
    metadata: dict | None = None,
) -> dict | None:
    """
    Insert a schedule claim. Returns the row on success, None if already claimed
    (unique violation / existing row). Safe for multi-instance coordination.
    """
    if not gmail_account_email or not job_kind or not period_key:
        raise ValueError("account, job_kind, and period_key are required")
    client = get_supabase_client()
    existing = (
        client.table("briefing_schedule_claims")
        .select("id,claimed_at")
        .eq("gmail_account_email", gmail_account_email)
        .eq("job_kind", job_kind)
        .eq("period_key", period_key)
        .limit(1)
        .execute()
    )
    if existing.data:
        return None
    try:
        result = (
            client.table("briefing_schedule_claims")
            .insert(
                {
                    "gmail_account_email": gmail_account_email,
                    "job_kind": job_kind,
                    "period_key": period_key,
                    "metadata": metadata or {},
                }
            )
            .execute()
        )
    except Exception:
        # Race: another instance claimed first
        return None
    if not result.data:
        return None
    return result.data[0]


def get_quality_counters(
    *,
    gmail_account_email: str,
    metric_date: str,
) -> dict | None:
    if not gmail_account_email or not metric_date:
        return None
    client = get_supabase_client()
    response = (
        client.table("briefing_quality_counters")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("metric_date", metric_date)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def bump_quality_counter(
    *,
    gmail_account_email: str,
    metric_date: str,
    metric: str,
    amount: int = 1,
) -> dict:
    if not gmail_account_email or not metric_date or not metric:
        raise ValueError("account, metric_date, and metric are required")
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    existing = get_quality_counters(
        gmail_account_email=gmail_account_email,
        metric_date=metric_date,
    )
    metrics = dict((existing or {}).get("metrics") or {})
    metrics[metric] = int(metrics.get(metric) or 0) + int(amount)
    row = {
        "gmail_account_email": gmail_account_email,
        "metric_date": metric_date,
        "metrics": metrics,
        "updated_at": now,
    }
    result = (
        client.table("briefing_quality_counters")
        .upsert(row, on_conflict="gmail_account_email,metric_date")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert briefing_quality_counters")
    return result.data[0]


def get_dashboard_visit(*, gmail_account_email: str) -> dict | None:
    if not gmail_account_email:
        return None
    client = get_supabase_client()
    response = (
        client.table("briefing_dashboard_visits")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_dashboard_visit(
    *,
    gmail_account_email: str,
    visited_at_iso: str,
) -> dict:
    """
    Record a meaningful Home visit.

    Moves current last_visit_at → previous_visit_at, then sets last_visit_at.
    """
    if not gmail_account_email:
        raise ValueError("gmail_account_email is required")
    client = get_supabase_client()
    existing = get_dashboard_visit(gmail_account_email=gmail_account_email)
    previous = (existing or {}).get("last_visit_at")
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "gmail_account_email": gmail_account_email,
        "last_visit_at": visited_at_iso,
        "previous_visit_at": previous,
        "updated_at": now,
        "metadata": (existing or {}).get("metadata") or {},
    }
    if not existing:
        row["created_at"] = now
    result = (
        client.table("briefing_dashboard_visits")
        .upsert(row, on_conflict="gmail_account_email")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert briefing_dashboard_visits")
    return result.data[0]


def get_catchup_cache(
    *,
    gmail_account_email: str,
    window_key: str,
) -> dict | None:
    if not gmail_account_email or not window_key:
        return None
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    response = (
        client.table("briefing_catchup_cache")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("window_key", window_key)
        .gt("expires_at", now)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def put_catchup_cache(
    *,
    gmail_account_email: str,
    window_key: str,
    payload: dict,
    ttl_minutes: int = 15,
) -> dict:
    if not gmail_account_email or not window_key:
        raise ValueError("account and window_key required")
    client = get_supabase_client()
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(minutes=max(1, int(ttl_minutes)))).isoformat()
    row = {
        "gmail_account_email": gmail_account_email,
        "window_key": window_key,
        "payload": payload,
        "expires_at": expires,
        "created_at": now.isoformat(),
    }
    result = (
        client.table("briefing_catchup_cache")
        .upsert(row, on_conflict="gmail_account_email,window_key")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert briefing_catchup_cache")
    return result.data[0]


def get_end_my_day_checkpoint(
    *,
    gmail_account_email: str,
    local_date: str,
) -> dict | None:
    """Closing checkpoint for one account and local date. None when absent."""
    if not gmail_account_email or not local_date:
        return None
    client = get_supabase_client()
    response = (
        client.table("end_my_day_checkpoints")
        .select("*")
        .eq("gmail_account_email", gmail_account_email)
        .eq("local_date", local_date)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def upsert_end_my_day_checkpoint(
    *,
    gmail_account_email: str,
    local_date: str,
    closed_at: str,
    timezone_name: str,
    summary: str,
    wrapup_source: str,
    item_refs: list,
    decisions: list,
    unresolved: list,
) -> dict:
    """Insert or update the closing checkpoint for one account and local date."""
    if not gmail_account_email or not local_date:
        raise ValueError("account and local_date are required")
    client = get_supabase_client()
    now = datetime.now(timezone.utc).isoformat()
    existing = get_end_my_day_checkpoint(
        gmail_account_email=gmail_account_email, local_date=local_date
    )
    row = {
        "gmail_account_email": gmail_account_email,
        "local_date": local_date,
        "closed_at": closed_at,
        "timezone": timezone_name,
        "summary": summary,
        "wrapup_source": wrapup_source or "stored_mail",
        "item_refs": item_refs or [],
        "decisions": decisions or [],
        "unresolved": unresolved or [],
        "updated_at": now,
    }
    if not existing:
        row["created_at"] = now
    result = (
        client.table("end_my_day_checkpoints")
        .upsert(row, on_conflict="gmail_account_email,local_date")
        .execute()
    )
    if not result.data:
        raise RuntimeError("Failed to upsert end_my_day_checkpoints")
    return result.data[0]
