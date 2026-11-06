"""In-memory counters for payload size. Development aid only.

Records byte counts and row counts. Does not store email bodies, query
text, OAuth tokens, or credentials.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

_operation: ContextVar[str] = ContextVar("egress_operation", default="")
_INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"

_lock = threading.Lock()
_supabase: dict[str, dict[str, int]] = defaultdict(
    lambda: {"queries": 0, "rows": 0, "bytes": 0}
)
_http: dict[str, dict[str, int]] = defaultdict(lambda: {"requests": 0, "bytes": 0})
_jobs: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0})

_LARGE_RESPONSE_BYTES = 500_000


def enabled() -> bool:
    raw = os.getenv("EGRESS_METER", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def instance_id() -> str:
    """Stable id for this process. Host and pid only — no tokens."""
    return _INSTANCE


@contextmanager
def using_operation(name: str):
    """Label the Supabase responses that happen inside this block."""
    token = _operation.set(name or "")
    try:
        yield
    finally:
        _operation.reset(token)


def record_supabase(table: str, rows: int, payload_bytes: int) -> None:
    """Count one Supabase response received by the backend."""
    if not enabled():
        return
    table_name = table or "unknown"
    operation = _operation.get()
    name = f"{operation}:{table_name}" if operation else table_name
    with _lock:
        bucket = _supabase[name]
        bucket["queries"] += 1
        bucket["rows"] += max(0, int(rows))
        bucket["bytes"] += max(0, int(payload_bytes))
    if payload_bytes >= _LARGE_RESPONSE_BYTES:
        logger.warning(
            "Large Supabase response table=%s rows=%s bytes=%s",
            name,
            rows,
            payload_bytes,
        )


def record_http(path: str, payload_bytes: int) -> None:
    """Count one JSON response sent from the backend to the browser."""
    if not enabled():
        return
    name = path or "unknown"
    with _lock:
        bucket = _http[name]
        bucket["requests"] += 1
        bucket["bytes"] += max(0, int(payload_bytes))


def record_job(name: str) -> None:
    if not enabled():
        return
    with _lock:
        _jobs[name or "unknown"]["runs"] += 1


def snapshot() -> dict:
    with _lock:
        supabase = {key: dict(value) for key, value in _supabase.items()}
        http = {key: dict(value) for key, value in _http.items()}
        jobs = {key: dict(value) for key, value in _jobs.items()}
    supabase_bytes = sum(item["bytes"] for item in supabase.values())
    http_bytes = sum(item["bytes"] for item in http.values())
    ranked = sorted(
        (
            {"operation": key, "queries": value["queries"], "rows": value["rows"], "bytes": value["bytes"]}
            for key, value in supabase.items()
        ),
        key=lambda item: item["bytes"],
        reverse=True,
    )
    return {
        "scheduler_instance": _INSTANCE,
        "supabase_to_backend": supabase,
        "supabase_to_backend_bytes": supabase_bytes,
        "top_operations": ranked[:12],
        "backend_to_browser": http,
        "backend_to_browser_bytes": http_bytes,
        "jobs": jobs,
    }


def reset() -> None:
    with _lock:
        _supabase.clear()
        _http.clear()
        _jobs.clear()


def payload_bytes(data) -> int:
    if data is None:
        return 0
    try:
        return len(json.dumps(data, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0
