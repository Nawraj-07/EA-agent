"""Read-only Gmail Inbox storage estimator.

Uses existing OAuth. Does not write Supabase, modify Gmail, call OpenAI,
or download attachments.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from typing import Any

from dotenv import load_dotenv

from googleapiclient.errors import HttpError

from gmail_sync import MAX_RETRIES, _build_service, _execute_with_backoff, list_messages_page
from main import extract_email_body, load_gmail_credentials

load_dotenv()

QUERY_START_EXCLUSIVE = date(2026, 5, 31)
SAMPLE_BODIES_DEFAULT = 40
METADATA_SAMPLE_DEFAULT = 300
INDEX_OVERHEAD_FACTOR = 0.40
ANALYSIS_ROW_BYTES = 1200
ROW_HEADER_BYTES = 32
ACCOUNT_EMAIL_BYTES = len("gaurang@rbhargavaassociates.in".encode("utf-8"))
UUID_BYTES = 16
BIGINT_BYTES = 8
BOOL_BYTES = 1


def inbox_query(as_of: date | None = None) -> str:
    today = as_of or date.today()
    before = today + timedelta(days=1)
    return (
        f"in:inbox after:{QUERY_START_EXCLUSIVE:%Y/%m/%d} "
        f"before:{before:%Y/%m/%d}"
    )


def _utf8_len(value: Any) -> int:
    if value is None:
        return 0
    return len(str(value).encode("utf-8"))


def _headers_map(payload: dict) -> dict[str, str]:
    return {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in (payload.get("headers") or [])
        if h.get("name")
    }


def _walk_parts(part: dict | None, attachments: list[int], text_parts: list[int]) -> None:
    if not part:
        return
    mime = str(part.get("mimeType") or "")
    filename = str(part.get("filename") or "")
    body = part.get("body") or {}
    size = int(body.get("size") or 0)
    if filename or body.get("attachmentId"):
        attachments.append(size)
    elif mime in {"text/plain", "text/html"} and size:
        text_parts.append(size)
    for child in part.get("parts") or []:
        _walk_parts(child, attachments, text_parts)


def _metadata_row(message: dict) -> dict[str, Any]:
    payload = message.get("payload") or {}
    headers = _headers_map(payload)
    attachments: list[int] = []
    text_parts: list[int] = []
    _walk_parts(payload, attachments, text_parts)
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "internal_date": message.get("internalDate"),
        "subject": headers.get("subject") or "",
        "sender": headers.get("from") or "",
        "date": headers.get("date") or "",
        "snippet": message.get("snippet") or "",
        "label_ids": message.get("labelIds") or [],
        "size_estimate": int(message.get("sizeEstimate") or 0),
        "attachment_bytes": sum(attachments),
        "attachment_count": len(attachments),
        "text_part_bytes": sum(text_parts),
    }


def _metadata_heap_bytes(row: dict[str, Any]) -> int:
    labels = row.get("label_ids") or []
    label_json = str(labels).replace("'", '"')
    return (
        ROW_HEADER_BYTES
        + UUID_BYTES
        + ACCOUNT_EMAIL_BYTES
        + _utf8_len(row.get("id"))
        + _utf8_len(row.get("thread_id"))
        + _utf8_len(row.get("subject"))
        + _utf8_len(row.get("sender"))
        + _utf8_len(row.get("date"))
        + _utf8_len(row.get("snippet"))
        + BIGINT_BYTES
        + _utf8_len(label_json)
        + BOOL_BYTES
        + _utf8_len("pending")
        + 8
    )


def _list_all_ids(credentials: Any, query: str) -> tuple[list[str], int | None]:
    ids: list[str] = []
    page_token = None
    estimate = None
    while True:
        page = list_messages_page(
            credentials,
            q=query,
            page_token=page_token,
            max_results=500,
        )
        ids.extend(page["message_ids"])
        if estimate is None:
            estimate = page.get("result_size_estimate")
        print(
            f"Listed {len(ids)} message ids...",
            file=sys.stderr,
            flush=True,
        )
        page_token = page.get("next_page_token")
        if not page_token:
            break
    return ids, estimate


def _get_metadata(service: Any, message_id: str) -> dict:
    return _execute_with_backoff(
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        )
    )


def _get_full(service: Any, message_id: str) -> dict:
    return _execute_with_backoff(
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
    )


def _pick_sample(ids: list[str], sample_size: int) -> list[str]:
    if not ids or sample_size <= 0:
        return []
    if sample_size >= len(ids):
        return list(ids)
    step = len(ids) / sample_size
    picked: list[str] = []
    seen: set[str] = set()
    for i in range(sample_size):
        message_id = ids[int(i * step)]
        if message_id not in seen:
            picked.append(message_id)
            seen.add(message_id)
    return picked


def _http_retryable(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return True
    status = getattr(exc.resp, "status", None)
    return status in {429, 500, 502, 503, 504}


def _execute_retrying(request_factory) -> dict:
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _execute_with_backoff(request_factory())
        except Exception as exc:
            last_exc = exc
            if _http_retryable(exc) and attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Gmail request failed")


def _fetch_metadata_sample(
    service: Any, ids: list[str], sample_size: int
) -> list[dict[str, Any]]:
    sample_ids = _pick_sample(ids, sample_size)
    rows: list[dict[str, Any]] = []
    skipped = 0
    total = len(sample_ids)
    for index, message_id in enumerate(sample_ids, start=1):
        try:
            message = _execute_retrying(
                lambda mid=message_id: service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
            )
            rows.append(_metadata_row(message))
        except Exception:
            skipped += 1
        if index % 25 == 0 or index == total:
            print(
                f"Sampled metadata {index}/{total}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(0.08)
    if skipped:
        print(f"Metadata sample skipped: {skipped}", file=sys.stderr, flush=True)
    return rows


def _sample_body_bytes(service: Any, ids: list[str], sample_size: int) -> dict[str, Any]:
    if not ids or sample_size <= 0:
        return {"sampled": 0, "avg_body_bytes": 0, "max_body_bytes": 0}
    sample_ids = _pick_sample(ids, sample_size)
    lengths: list[int] = []
    skipped = 0
    for index, message_id in enumerate(sample_ids, start=1):
        try:
            message = _execute_retrying(
                lambda mid=message_id: service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
            )
            body = extract_email_body(message.get("payload") or {})
            lengths.append(len(body.encode("utf-8")))
        except Exception:
            skipped += 1
        print(
            f"Sampled body {index}/{len(sample_ids)}",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(0.12)
    if skipped:
        print(f"Body sample skipped: {skipped}", file=sys.stderr, flush=True)
    avg = int(round(sum(lengths) / len(lengths))) if lengths else 0
    return {
        "sampled": len(lengths),
        "avg_body_bytes": avg,
        "max_body_bytes": max(lengths) if lengths else 0,
    }


def _fmt_bytes(num: int | float) -> str:
    mb = num / (1024 * 1024)
    gb = num / (1024 * 1024 * 1024)
    return f"{num:,.0f} bytes | {mb:.2f} MB | {gb:.4f} GB"


def run(sample_size: int, metadata_sample: int) -> int:
    query = inbox_query()
    print(f"Query: {query}", file=sys.stderr, flush=True)
    credentials = load_gmail_credentials()
    service = _build_service(credentials)

    ids, list_estimate = _list_all_ids(credentials, query)
    count = len(ids)
    print("Waiting 20s for Gmail quota...", file=sys.stderr, flush=True)
    time.sleep(20)
    rows = _fetch_metadata_sample(service, ids, metadata_sample) if ids else []
    sample = _sample_body_bytes(service, ids, sample_size)

    sampled = len(rows)
    if sampled == 0 and count:
        print("Estimator failed: no metadata sample", file=sys.stderr)
        return 1

    avg_gmail = (sum(int(r["size_estimate"]) for r in rows) / sampled) if sampled else 0
    attach_rate = (
        sum(1 for r in rows if r["attachment_count"]) / sampled if sampled else 0
    )
    avg_attach = (
        sum(int(r["attachment_bytes"]) for r in rows) / sampled if sampled else 0
    )
    avg_meta = (
        sum(_metadata_heap_bytes(r) for r in rows) / sampled if sampled else 0
    )
    gmail_total = avg_gmail * count
    attachment_total = avg_attach * count
    attachment_messages = int(round(attach_rate * count))
    metadata_heap = avg_meta * count
    body_total = sample["avg_body_bytes"] * count
    meta_plus_body = metadata_heap + body_total
    analysis_total = ANALYSIS_ROW_BYTES * count
    meta_body_analysis = meta_plus_body + analysis_total
    with_indexes = int(round(meta_body_analysis * (1 + INDEX_OVERHEAD_FACTOR)))

    print()
    print("Inbox storage estimate (read-only; nothing stored)")
    print(f"Account: gaurang@rbhargavaassociates.in")
    print(f"Gmail query: {query}")
    print(f"List resultSizeEstimate: {list_estimate}")
    print(f"Messages listed: {count}")
    print(f"Metadata sampled: {sampled} (averages extrapolated to {count})")
    print()
    print("Gmail raw MIME (sizeEstimate, includes attachments/HTML)")
    print(f"  Total:   {_fmt_bytes(gmail_total)}")
    print(f"  Average: {_fmt_bytes(avg_gmail)}" if sampled else "  Average: n/a")
    print(
        f"  Attachment parts (metadata body.size, not downloaded): "
        f"{_fmt_bytes(attachment_total)} across ~{attachment_messages} messages"
    )
    print()
    print("Buddy / PostgreSQL (actual emails + email_analyses schema)")
    print(f"  Metadata only (emails row without body): {_fmt_bytes(metadata_heap)}")
    print(f"  Average metadata row: {_fmt_bytes(avg_meta)}" if sampled else "")
    print(
        f"  Extracted text body (sample {sample['sampled']} messages, "
        f"avg {sample['avg_body_bytes']:,} bytes, "
        f"max {sample['max_body_bytes']:,}): {_fmt_bytes(body_total)}"
    )
    print(f"  Metadata + body: {_fmt_bytes(meta_plus_body)}")
    print(
        f"  AI analysis rows (assumed {ANALYSIS_ROW_BYTES} bytes each, "
        f"no OpenAI call): {_fmt_bytes(analysis_total)}"
    )
    print(f"  Metadata + body + analysis: {_fmt_bytes(meta_body_analysis)}")
    print(
        f"  Plus ~{int(INDEX_OVERHEAD_FACTOR * 100)}% PK/UNIQUE/secondary indexes: "
        f"{_fmt_bytes(with_indexes)}"
    )
    print()
    print("Assumptions")
    print("  - Window is Inbox only, not Sent/All Mail.")
    print("  - Buddy stores extracted plaintext, not raw MIME or attachments.")
    print("  - sizeEstimate and metadata row size are extrapolated from a spaced sample.")
    print("  - Body size is extrapolated from a spaced sample of format=full.")
    print("  - Analysis size is schema-based (typical summary width), not generated.")
    print("  - Index factor 40% covers three emails indexes + unique constraint.")
    print("  - No drafts or daily_briefings included.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Estimate Inbox storage from 1 Jun 2026 through today."
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=SAMPLE_BODIES_DEFAULT,
        help="Number of full messages to sample for extracted body size",
    )
    parser.add_argument(
        "--metadata-sample",
        type=int,
        default=METADATA_SAMPLE_DEFAULT,
        help="Number of messages to sample for sizeEstimate and row metadata",
    )
    args = parser.parse_args()
    try:
        return run(max(0, args.sample_size), max(0, args.metadata_sample))
    except Exception as exc:
        print(f"Estimator failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
