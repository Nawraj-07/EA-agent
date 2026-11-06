"""AI Follow-up Assistant — enhanced unanswered-outgoing tracking (additive).

Feature flag: AI_FOLLOWUP_ASSISTANT_ENABLED (default false).

When disabled:
  - Task 2 pending-reply detection is unchanged
  - No extra background processing or UI actions

When enabled:
  - Threshold-gated awaiting_response recommendations
  - Auto-reply aware resolution
  - Attempt counts from verified SENT messages (not drafts)
  - Sync into briefing_recommendation_state (source_type=followup)
  - Soft refresh after Gmail sync

Does not send email or WhatsApp. Does not modify Gmail messages.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

AUTO_REPLY_RE = re.compile(
    r"\b(?:auto[\s-]?reply|automatic reply|out of office|away from (?:the )?office|"
    r"vacation (?:reply|response)|ooo\b|i am currently (?:out|away)|"
    r"this is an automated)\b",
    re.I,
)
REQUEST_RE = re.compile(
    r"\?|\b(?:please\s+)?(?:confirm|approve|advise|let\s+me\s+know|could\s+you|"
    r"can\s+you|would\s+you|need\s+your|awaiting\s+your|looking\s+forward|"
    r"kindly\s+(?:share|send|confirm|approve)|request(?:ing)?\s+your)\b",
    re.I,
)

DEFAULT_THRESHOLDS_HOURS = {"high": 24, "medium": 48, "low": 72}


def ai_followup_assistant_enabled() -> bool:
    return os.getenv("AI_FOLLOWUP_ASSISTANT_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def followup_actions_enabled() -> bool:
    """Draft / snooze / dismiss for follow-ups (assistant or interactive Task 4)."""
    if ai_followup_assistant_enabled():
        return True
    try:
        from briefing_recommendations import interactive_briefing_enabled

        return interactive_briefing_enabled()
    except Exception:
        return False


def threshold_hours_for_priority(priority: str | None) -> int:
    key = str(priority or "medium").strip().lower()
    if key not in DEFAULT_THRESHOLDS_HOURS:
        key = "medium"
    env_key = {
        "high": "AI_FOLLOWUP_THRESHOLD_HIGH_HOURS",
        "medium": "AI_FOLLOWUP_THRESHOLD_MEDIUM_HOURS",
        "low": "AI_FOLLOWUP_THRESHOLD_LOW_HOURS",
    }[key]
    raw = os.getenv(env_key, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_THRESHOLDS_HOURS[key]


def stable_followup_key(
    *,
    account_email: str,
    gmail_thread_id: str | None,
    request_message_id: str | None,
) -> str:
    raw = "|".join(
        [
            (account_email or "").strip().lower(),
            "followup",
            (gmail_thread_id or "").strip(),
            (request_message_id or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def is_automatic_reply(email: dict | None) -> bool:
    if not isinstance(email, dict):
        return False
    blob = " ".join(
        str(email.get(key) or "")
        for key in ("subject", "snippet", "body")
    )
    subject = str(email.get("subject") or "")
    if AUTO_REPLY_RE.search(blob) or AUTO_REPLY_RE.search(subject):
        return True
    labels = email.get("label_ids") or []
    if isinstance(labels, list) and any(
        str(label).upper() in {"CATEGORY_FORUMS", "CATEGORY_PROMOTIONS"}
        for label in labels
    ):
        # Not definitive auto-reply; leave to classifier
        pass
    return False


def looks_like_outgoing_request(email: dict, analysis: dict | None = None) -> bool:
    """True when the outgoing message reasonably expects a response."""
    analysis = analysis or {}
    blob = " ".join(
        str(email.get(key) or "")
        for key in ("subject", "snippet", "body")
    )
    summary = str(analysis.get("summary") or "")
    if REQUEST_RE.search(blob) or REQUEST_RE.search(summary):
        return True
    action = analysis.get("action_required")
    # Outgoing with action_required rarely set; do not invent
    if action is True and REQUEST_RE.search(blob + " " + summary):
        return True
    return False


def looks_like_informational_outgoing(email: dict, analysis: dict | None = None) -> bool:
    analysis = analysis or {}
    if looks_like_outgoing_request(email, analysis):
        return False
    blob = " ".join(
        str(email.get(key) or "")
        for key in ("subject", "snippet", "body")
    ).lower()
    informational_markers = (
        "fyi",
        "for your information",
        "no action needed",
        "no reply needed",
        "newsletter",
        "unsubscribe",
    )
    if any(marker in blob for marker in informational_markers):
        return True
    if analysis.get("action_required") is False:
        return True
    return False


def message_epoch_ms(email: dict) -> int:
    raw = email.get("internal_date")
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        value = 0
    if value > 10_000_000_000:  # already ms
        return value
    if value > 0:
        return value * 1000
    received = email.get("received_at") or email.get("sent_at")
    if received:
        try:
            text = str(received).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def waiting_duration_hours(
    last_outgoing: dict,
    *,
    now: datetime | None = None,
) -> float:
    ms = message_epoch_ms(last_outgoing)
    if ms <= 0:
        return 0.0
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    sent = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return max(0.0, (current - sent).total_seconds() / 3600.0)


def threshold_elapsed(
    *,
    priority: str | None,
    waiting_hours: float,
    explicit_deadline: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if explicit_deadline is not None:
        deadline = explicit_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return current >= deadline
    return waiting_hours >= float(threshold_hours_for_priority(priority))


def count_verified_followup_attempts(
    messages: list[dict],
    *,
    account_email: str,
    request_message_id: str | None,
    message_direction_fn,
) -> int:
    """
    Count outgoing SENT messages after the original request message.

    Generated drafts are never counted — only stored Gmail messages.
    """
    if not messages:
        return 0
    sorted_msgs = sorted(
        [row for row in messages if isinstance(row, dict)],
        key=lambda row: (message_epoch_ms(row), str(row.get("id") or "")),
    )
    start_ms = 0
    if request_message_id:
        for row in sorted_msgs:
            if str(row.get("id") or "") == str(request_message_id):
                start_ms = message_epoch_ms(row)
                break
    attempts = 0
    for row in sorted_msgs:
        if message_epoch_ms(row) <= start_ms:
            continue
        if message_direction_fn(row, account_email) != "outgoing":
            continue
        labels = row.get("label_ids") or []
        label_set = {str(item) for item in labels} if isinstance(labels, list) else set()
        if "DRAFT" in label_set:
            continue
        # Count verified Gmail messages only (SENT label or From==account already
        # established by message_direction_fn). Never count local draft rows.
        attempts += 1
    return attempts


def relevant_incoming_response(
    messages: list[dict],
    *,
    account_email: str,
    after_ms: int,
    message_direction_fn,
) -> dict | None:
    """
    Find a relevant human incoming reply after the outgoing ask.

    Automatic replies do not count as resolution.
    """
    sorted_msgs = sorted(
        [row for row in messages if isinstance(row, dict)],
        key=lambda row: (message_epoch_ms(row), str(row.get("id") or "")),
    )
    for row in sorted_msgs:
        if message_epoch_ms(row) <= after_ms:
            continue
        if message_direction_fn(row, account_email) != "incoming":
            continue
        if is_automatic_reply(row):
            continue
        return row
    return None


def sync_followup_recommendation_states(
    *,
    account_email: str,
    followup_items: list[dict[str, Any]],
    resolved_keys: list[str] | None = None,
) -> dict[str, Any]:
    """
    Upsert active follow-ups and mark resolved keys completed.

    Soft-fails when briefing_recommendation_state is missing.
    """
    stats = {"upserted": 0, "resolved": 0, "skipped": 0, "error": None}
    if not ai_followup_assistant_enabled():
        return stats
    if not account_email:
        return stats

    try:
        from db import (
            get_recommendation_state,
            release_expired_recommendation_snoozes,
            upsert_recommendation_state,
        )

        release_expired_recommendation_snoozes(
            gmail_account_email=account_email,
            now_iso=datetime.now(timezone.utc).isoformat(),
        )

        for item in followup_items:
            key = str(item.get("recommendation_key") or "").strip()
            if not key:
                stats["skipped"] += 1
                continue
            existing = get_recommendation_state(
                gmail_account_email=account_email,
                recommendation_key=key,
            )
            if existing and str(existing.get("status") or "") in {
                "dismissed",
                "completed",
            }:
                # Preserve dismissal / resolution unless a new request key appears
                stats["skipped"] += 1
                continue
            if existing and str(existing.get("status") or "") == "snoozed":
                # Keep snooze; metadata can still refresh attempt counts
                meta = dict(existing.get("metadata") or {})
                meta.update(
                    {
                        "attempt_count": item.get("followup_attempt_count"),
                        "waiting_hours": item.get("waiting_hours"),
                        "followup_state": item.get("followup_state"),
                        "source_email_id": item.get("source_email_id"),
                        "gmail_thread_id": item.get("gmail_thread_id"),
                    }
                )
                upsert_recommendation_state(
                    gmail_account_email=account_email,
                    source_type="followup",
                    source_id=str(
                        item.get("source_email_id")
                        or item.get("gmail_thread_id")
                        or key
                    ),
                    recommendation_key=key,
                    status="snoozed",
                    snoozed_until=existing.get("snoozed_until"),
                    title=item.get("title"),
                    action_type="snooze",
                    metadata=meta,
                )
                stats["upserted"] += 1
                continue

            upsert_recommendation_state(
                gmail_account_email=account_email,
                source_type="followup",
                source_id=str(
                    item.get("source_email_id")
                    or item.get("gmail_thread_id")
                    or key
                ),
                recommendation_key=key,
                status="pending",
                snoozed_until=None,
                title=item.get("title"),
                action_type=None,
                metadata={
                    "attempt_count": item.get("followup_attempt_count"),
                    "waiting_hours": item.get("waiting_hours"),
                    "followup_state": item.get("followup_state"),
                    "followup_confidence": item.get("followup_confidence"),
                    "source_email_id": item.get("source_email_id"),
                    "gmail_thread_id": item.get("gmail_thread_id"),
                    "recipient": item.get("recipient") or item.get("sender"),
                    "last_outgoing_at": item.get("last_outgoing_at"),
                },
            )
            stats["upserted"] += 1

        for key in resolved_keys or []:
            key = str(key or "").strip()
            if not key:
                continue
            existing = get_recommendation_state(
                gmail_account_email=account_email,
                recommendation_key=key,
            )
            if not existing:
                continue
            if str(existing.get("status") or "") == "completed":
                continue
            meta = dict(existing.get("metadata") or {})
            meta["resolved_reason"] = "relevant_incoming_response"
            upsert_recommendation_state(
                gmail_account_email=account_email,
                source_type=str(existing.get("source_type") or "followup"),
                source_id=str(existing.get("source_id") or key),
                recommendation_key=key,
                status="completed",
                snoozed_until=None,
                title=existing.get("title"),
                action_type="mark_complete",
                metadata=meta,
            )
            stats["resolved"] += 1
    except Exception as exc:
        message = str(exc)
        if "briefing_recommendation_state" in message or "PGRST205" in message:
            stats["error"] = "recommendation_state_table_missing"
            logger.warning(
                "Follow-up state sync skipped: briefing_recommendation_state missing"
            )
        else:
            stats["error"] = "sync_failed"
            logger.exception("Follow-up recommendation state sync failed")
    return stats


def suppressed_followup_keys(account_email: str) -> set[str]:
    """Keys that should not appear (dismissed / completed / still snoozed)."""
    if not account_email or not ai_followup_assistant_enabled():
        return set()
    try:
        from db import (
            list_recommendation_states_for_account,
            release_expired_recommendation_snoozes,
        )

        release_expired_recommendation_snoozes(
            gmail_account_email=account_email,
            now_iso=datetime.now(timezone.utc).isoformat(),
        )
        suppressed: set[str] = set()
        now = datetime.now(timezone.utc)
        for row in list_recommendation_states_for_account(
            gmail_account_email=account_email
        ):
            if str(row.get("source_type") or "") != "followup":
                continue
            status = str(row.get("status") or "")
            key = str(row.get("recommendation_key") or "")
            if not key:
                continue
            if status in {"dismissed", "completed"}:
                suppressed.add(key)
            elif status == "snoozed":
                until = row.get("snoozed_until")
                if until:
                    try:
                        text = str(until)
                        if text.endswith("Z"):
                            text = text[:-1] + "+00:00"
                        when = datetime.fromisoformat(text)
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=timezone.utc)
                        if when > now:
                            suppressed.add(key)
                    except Exception:
                        suppressed.add(key)
                else:
                    suppressed.add(key)
        return suppressed
    except Exception:
        logger.exception("Failed loading suppressed follow-up keys")
        return set()


def refresh_followups_after_sync(account_email: str) -> dict[str, Any]:
    """
    Soft refresh after Gmail sync: re-detect and resolve without OpenAI.

    Never raises into the sync pipeline.
    """
    result: dict[str, Any] = {
        "enabled": ai_followup_assistant_enabled(),
        "processed": 0,
        "active": 0,
        "resolved": 0,
        "error": None,
    }
    if not ai_followup_assistant_enabled():
        return result
    if not account_email:
        result["error"] = "missing_account"
        return result
    try:
        from briefing_followup import detect_followups_for_account_sync
        from db import EMAIL_LIST_WITH_ANALYSIS, get_supabase_client
        from egress_meter import using_operation

        # Recent metadata only. Reply detection loads a thread body when it
        # classifies that thread, so this scan does not pull 80 full bodies.
        client = get_supabase_client()
        with using_operation("followup_refresh"):
            response = (
                client.table("emails")
                .select(EMAIL_LIST_WITH_ANALYSIS)
                .eq("gmail_account_email", account_email)
                .order("internal_date", desc=True)
                .limit(80)
                .execute()
            )
            emails = response.data or []
            result["processed"] = len(emails)
            detected = detect_followups_for_account_sync(
                emails,
                account_email=account_email,
                gmail_account_email=account_email,
            )
        active = detected.get("active") or []
        resolved_keys = detected.get("resolved_keys") or []
        sync_stats = sync_followup_recommendation_states(
            account_email=account_email,
            followup_items=active,
            resolved_keys=resolved_keys,
        )
        result["active"] = len(active)
        result["resolved"] = sync_stats.get("resolved", 0)
        result["sync"] = sync_stats
    except Exception as exc:
        result["error"] = str(exc)[:200]
        logger.exception(
            "Follow-up refresh after sync failed account=%s", account_email
        )
    return result


def followup_draft_instruction() -> str:
    return (
        "Write a polite, concise follow-up email that restates the outstanding "
        "request without inventing commitments, deadlines, or attachments. "
        "Do not claim a prior deadline unless it appears in the thread."
    )
