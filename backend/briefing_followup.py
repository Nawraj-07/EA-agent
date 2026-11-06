"""Pending-reply / follow-up detection for Smart Daily Briefing Task 2.

Uses stored Gmail thread messages, analysis fields, and sent drafts.
Does not call OpenAI for state classification.
Does not change OAuth scopes (gmail.readonly already covers thread reads).

Limitation: sync is primarily INBOX. Outgoing-only SENT mail is often absent,
so awaiting_response is only asserted when outgoing evidence exists in-store
(or via optional Gmail thread verify). Incomplete threads stay unknown.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any, Literal

from briefing_structure import _analysis, build_overview, sender_display

logger = logging.getLogger(__name__)

ConversationState = Literal[
    "pending_reply",
    "awaiting_response",
    "resolved",
    "informational",
    "unknown",
]

MAX_FOLLOWUPS = 3
MAX_BRIEFING_ITEMS = 6

EMAIL_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
QUESTION_RE = re.compile(
    r"\?|\b(?:please\s+)?(?:confirm|advise|let\s+me\s+know|could\s+you|"
    r"can\s+you|would\s+you|need\s+your|awaiting\s+your|looking\s+forward)\b",
    re.I,
)
NO_REPLY_SENDER_RE = re.compile(
    r"noreply|no-reply|donotreply|do-not-reply|notifications?@|"
    r"mailer-daemon|newsletter|news@|updates@",
    re.I,
)


def pending_reply_detection_enabled() -> bool:
    return os.getenv("PENDING_REPLY_DETECTION_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def followup_detection_active() -> bool:
    """Task 2 flag or AI Follow-up Assistant — either may run detection."""
    if pending_reply_detection_enabled():
        return True
    try:
        from ai_followup_assistant import ai_followup_assistant_enabled

        return ai_followup_assistant_enabled()
    except Exception:
        return False


def _enhanced_assistant_enabled() -> bool:
    try:
        from ai_followup_assistant import ai_followup_assistant_enabled

        return ai_followup_assistant_enabled()
    except Exception:
        return False


def normalize_email_address(raw: str | None) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    match = EMAIL_ADDR_RE.search(text)
    return match.group(0) if match else text


def _label_ids(email: dict) -> set[str]:
    raw = email.get("label_ids") or []
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item is not None}


def message_direction(email: dict, account_email: str) -> Literal["incoming", "outgoing", "unknown"]:
    """
    Infer direction from labels and From vs authenticated account.

    SENT label or From==account → outgoing.
    Otherwise a non-empty foreign From → incoming.
    """
    labels = _label_ids(email)
    if "SENT" in labels:
        return "outgoing"
    if "DRAFT" in labels:
        return "unknown"

    account = normalize_email_address(account_email)
    sender = normalize_email_address(email.get("sender") or email.get("from"))
    if account and sender and sender == account:
        return "outgoing"
    if sender:
        return "incoming"
    return "unknown"


def _internal_date(email: dict) -> int:
    raw = email.get("internal_date")
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def _sort_thread(messages: list[dict]) -> list[dict]:
    return sorted(
        [row for row in messages if isinstance(row, dict)],
        key=lambda row: (_internal_date(row), str(row.get("id") or "")),
    )


def _looks_informational(email: dict, analysis: dict) -> bool:
    sender = str(email.get("sender") or "")
    if NO_REPLY_SENDER_RE.search(sender):
        return True
    importance = str(analysis.get("importance") or "").strip().lower()
    action = analysis.get("action_required")
    if action is False and importance == "low":
        return True
    category = str(analysis.get("category") or "").strip().lower()
    if category == "other" and action is False:
        return True
    return False


def _asks_for_response(email: dict, analysis: dict) -> bool:
    blob = " ".join(
        str(email.get(key) or "")
        for key in ("subject", "snippet", "body")
    )
    summary = str(analysis.get("summary") or "")
    return bool(QUESTION_RE.search(blob) or QUESTION_RE.search(summary))


def _action_required(analysis: dict) -> bool | None:
    raw = analysis.get("action_required")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return None


def classify_thread_state(
    messages: list[dict],
    *,
    account_email: str,
    sent_cleared: bool = False,
) -> dict[str, Any]:
    """
    Classify one conversation from stored messages.

    sent_cleared: True when Buddy recorded a sent draft for this thread
    (clears pending_reply even if SENT mail is not synced into emails).
    """
    thread = _sort_thread(messages)
    if not thread:
        return {
            "state": "unknown",
            "confidence": "low",
            "reason": "No stored messages for this thread.",
            "source_email_id": None,
            "gmail_thread_id": None,
        }

    thread_id = thread[-1].get("gmail_thread_id") or thread[-1].get("thread_id")
    directions = [(message_direction(msg, account_email), msg) for msg in thread]
    known = [(direction, msg) for direction, msg in directions if direction != "unknown"]

    if not known:
        return {
            "state": "unknown",
            "confidence": "low",
            "reason": "Message direction could not be determined from stored data.",
            "source_email_id": thread[-1].get("id"),
            "gmail_thread_id": thread_id,
        }

    # Incomplete sync: only outgoing messages stored (unusual for INBOX sync)
    if all(direction == "outgoing" for direction, _msg in known) and len(known) < 2:
        last_out = known[-1][1]
        analysis = _analysis(last_out)
        if sent_cleared:
            return {
                "state": "resolved",
                "confidence": "medium",
                "reason": "Outgoing message recorded; treating as handled.",
                "source_email_id": last_out.get("id"),
                "gmail_thread_id": thread_id,
            }
        if _asks_for_response(last_out, analysis):
            return {
                "state": "awaiting_response",
                "confidence": "low",
                "reason": (
                    "Outgoing ask found in store, but conversation may be "
                    "incomplete without later replies."
                ),
                "source_email_id": last_out.get("id"),
                "gmail_thread_id": thread_id,
            }
        return {
            "state": "unknown",
            "confidence": "low",
            "reason": "Only outgoing messages are stored for this thread.",
            "source_email_id": last_out.get("id"),
            "gmail_thread_id": thread_id,
        }

    last_direction, last_msg = known[-1]
    last_analysis = _analysis(last_msg)
    last_action = _action_required(last_analysis)

    # Find previous opposite-direction evidence
    earlier = known[:-1]
    has_earlier_incoming = any(d == "incoming" for d, _ in earlier)
    has_earlier_outgoing = any(d == "outgoing" for d, _ in earlier)

    if last_direction == "outgoing":
        if sent_cleared or has_earlier_incoming:
            # User replied after inbound mail → pending cleared
            if _asks_for_response(last_msg, last_analysis) and not _has_later_incoming(
                known, after_index=len(known) - 1
            ):
                # Last event is still our ask with no later inbound in store
                return {
                    "state": "awaiting_response",
                    "confidence": "medium" if "SENT" in _label_ids(last_msg) else "low",
                    "reason": (
                        "Latest stored message is outgoing and appears to ask "
                        "for a response; no later incoming reply in store."
                    ),
                    "source_email_id": last_msg.get("id"),
                    "gmail_thread_id": thread_id,
                }
            return {
                "state": "resolved",
                "confidence": "high" if has_earlier_incoming or sent_cleared else "medium",
                "reason": (
                    "Latest stored message is outgoing after inbound mail "
                    "(or a sent draft was recorded)."
                ),
                "source_email_id": last_msg.get("id"),
                "gmail_thread_id": thread_id,
            }
        return {
            "state": "unknown",
            "confidence": "low",
            "reason": "Outgoing message without enough inbound history in store.",
            "source_email_id": last_msg.get("id"),
            "gmail_thread_id": thread_id,
        }

    # last_direction == incoming
    if sent_cleared and has_earlier_outgoing:
        # Draft sent after this inbound? sent_cleared means user sent something on thread
        # If last is still incoming AFTER sent clear timestamps, keep pending.
        pass

    # Automatic replies after our ask do not resolve awaiting_response
    try:
        from ai_followup_assistant import is_automatic_reply

        if is_automatic_reply(last_msg) and has_earlier_outgoing:
            last_out = next(
                (
                    msg
                    for direction, msg in reversed(known[:-1])
                    if direction == "outgoing"
                ),
                None,
            )
            return {
                "state": "awaiting_response",
                "confidence": "medium",
                "reason": (
                    "Latest inbound looks like an automatic reply; "
                    "original request still awaiting a human response."
                ),
                "source_email_id": (last_out or last_msg).get("id"),
                "gmail_thread_id": thread_id,
            }
    except Exception:
        pass

    if _looks_informational(last_msg, last_analysis) and last_action is not True:
        return {
            "state": "informational",
            "confidence": "medium",
            "reason": "Incoming message appears informational / no-reply.",
            "source_email_id": last_msg.get("id"),
            "gmail_thread_id": thread_id,
        }

    if last_action is True:
        return {
            "state": "pending_reply",
            "confidence": "high",
            "reason": "Latest inbound message is marked action_required.",
            "source_email_id": last_msg.get("id"),
            "gmail_thread_id": thread_id,
        }

    if last_action is False:
        return {
            "state": "informational",
            "confidence": "medium",
            "reason": "Latest inbound message has action_required=false.",
            "source_email_id": last_msg.get("id"),
            "gmail_thread_id": thread_id,
        }

    # Missing analysis on latest inbound
    if has_earlier_outgoing:
        # Someone replied after we wrote — not necessarily needing our reply
        return {
            "state": "unknown",
            "confidence": "low",
            "reason": (
                "Inbound message after our outbound exists, but analysis is "
                "insufficient to decide if a reply is needed."
            ),
            "source_email_id": last_msg.get("id"),
            "gmail_thread_id": thread_id,
        }

    importance = str(last_analysis.get("importance") or "").lower()
    if importance == "high":
        return {
            "state": "pending_reply",
            "confidence": "medium",
            "reason": "High-importance inbound message without a later outgoing reply in store.",
            "source_email_id": last_msg.get("id"),
            "gmail_thread_id": thread_id,
        }

    return {
        "state": "unknown",
        "confidence": "low",
        "reason": "Insufficient analysis to confirm a required reply.",
        "source_email_id": last_msg.get("id"),
        "gmail_thread_id": thread_id,
    }


def _has_later_incoming(
    known: list[tuple[str, dict]], *, after_index: int
) -> bool:
    for direction, _msg in known[after_index + 1 :]:
        if direction == "incoming":
            return True
    return False


def _sent_draft_thread_ids(gmail_account_email: str) -> set[str]:
    """Threads where Buddy already recorded a successful send."""
    try:
        from db import get_supabase_client

        client = get_supabase_client()
        drafts = (
            client.table("drafts")
            .select("email_id,status,gmail_sent_message_id")
            .eq("status", "sent")
            .limit(200)
            .execute()
        )
        email_ids = [
            str(row.get("email_id"))
            for row in (drafts.data or [])
            if row.get("email_id")
        ]
        if not email_ids:
            return set()
        emails = (
            client.table("emails")
            .select("id,gmail_thread_id,gmail_account_email")
            .eq("gmail_account_email", gmail_account_email)
            .in_("id", email_ids)
            .execute()
        )
        return {
            str(row.get("gmail_thread_id"))
            for row in (emails.data or [])
            if row.get("gmail_thread_id")
        }
    except Exception:
        logger.exception("Failed loading sent-draft thread ids for follow-up detection")
        return set()


def _load_thread_messages(
    *,
    account_key: str,
    thread_id: str,
    seed_messages: list[dict],
    list_thread_messages=None,
) -> list[dict]:
    messages = list(seed_messages)
    loader = list_thread_messages
    if loader is None:
        try:
            from db import list_thread_messages as db_list_thread_messages

            def loader(*, gmail_account_email: str, thread_id: str):
                if thread_id.startswith("msg:"):
                    return []
                return db_list_thread_messages(
                    gmail_account_email=gmail_account_email,
                    thread_id=thread_id,
                    include_body=False,
                )
        except Exception:
            loader = None

    if loader is not None and account_key and not thread_id.startswith("msg:"):
        try:
            loaded = loader(
                gmail_account_email=account_key,
                thread_id=thread_id,
            )
            if loaded:
                by_id = {
                    str(row.get("id") or row.get("gmail_message_id") or ""): row
                    for row in messages
                }
                for row in loaded:
                    key = str(row.get("id") or row.get("gmail_message_id") or "")
                    if key:
                        by_id[key] = row
                    else:
                        messages.append(row)
                messages = list(by_id.values()) or messages
        except Exception:
            logger.exception(
                "Thread load failed thread_id=%s; using seed messages only",
                thread_id,
            )
    _hydrate_reply_bodies(messages)
    return messages


def _rows_needing_reply_body(messages: list[dict]) -> list[dict]:
    """Bodies are loaded only when snippet and summary do not already decide."""
    from briefing_structure import _analysis

    needed: list[dict] = []
    for row in messages:
        if not isinstance(row, dict) or "body" in row:
            continue
        analysis = _analysis(row) or {}
        blob = " ".join(str(row.get(key) or "") for key in ("subject", "snippet"))
        summary = str(analysis.get("summary") or "")
        if QUESTION_RE.search(blob) or QUESTION_RE.search(summary):
            continue
        if analysis.get("action_required") is True:
            needed.append(row)
    return needed


def _hydrate_reply_bodies(messages: list[dict]) -> None:
    needed = _rows_needing_reply_body(messages)
    if not needed:
        return
    try:
        from db import hydrate_email_bodies

        hydrate_email_bodies(needed)
    except Exception:
        logger.exception("Could not load reply bodies; using snippet and summary")


def _build_followup_item(
    *,
    state: str,
    classified: dict,
    source: dict,
    messages: list[dict],
    account: str,
    thread_id: str,
    enhanced: bool,
) -> dict[str, Any] | None:
    subject = str(source.get("subject") or "Conversation").strip()
    sender = sender_display(source.get("sender"))
    analysis = _analysis(source)
    priority = (
        str(analysis.get("importance") or "medium").lower()
        if str(analysis.get("importance") or "").lower() in {"high", "medium", "low"}
        else "medium"
    )
    gmail_thread_id = (
        thread_id
        if not thread_id.startswith("msg:")
        else source.get("gmail_thread_id")
    )

    if state == "pending_reply":
        title = f"Reply needed: {subject}"[:120]
        action = f"Reply to {sender or 'the sender'} regarding “{subject}”."
        kind = "followup" if enhanced else "action"
        recipient = sender
        request_id = str(source.get("id") or "")
        waiting_hours = 0.0
        attempt_count = 0
        last_outgoing_at = ""
        evidence = classified.get("confidence") or "medium"
    else:
        # awaiting_response — outgoing ask
        title = f"Follow up: {subject}"[:120]
        action = f"Follow up on “{subject}” — still awaiting their response."
        kind = "followup" if enhanced else "action"
        recipient = sender_display(
            source.get("to") or source.get("recipient") or source.get("sender")
        )
        request_id = str(source.get("id") or "")
        waiting_hours = 0.0
        attempt_count = 0
        last_outgoing_at = ""
        evidence = classified.get("confidence") or "medium"

        if enhanced:
            try:
                from ai_followup_assistant import (
                    count_verified_followup_attempts,
                    looks_like_informational_outgoing,
                    looks_like_outgoing_request,
                    relevant_incoming_response,
                    stable_followup_key,
                    threshold_elapsed,
                    waiting_duration_hours,
                    message_epoch_ms,
                    is_automatic_reply,
                )

                if looks_like_informational_outgoing(source, analysis):
                    return None
                if not looks_like_outgoing_request(source, analysis):
                    # Incomplete evidence → uncertain, not a definitive follow-up
                    return {
                        "_uncertain": True,
                        "subject": subject,
                        "gmail_thread_id": gmail_thread_id,
                        "source_email_id": source.get("id"),
                        "reason": "Outgoing message does not clearly request a response.",
                        "confidence": "low",
                    }

                after_ms = message_epoch_ms(source)
                reply = relevant_incoming_response(
                    messages,
                    account_email=account,
                    after_ms=after_ms,
                    message_direction_fn=message_direction,
                )
                if reply is not None:
                    return {
                        "_resolved": True,
                        "recommendation_key": stable_followup_key(
                            account_email=account,
                            gmail_thread_id=str(gmail_thread_id or ""),
                            request_message_id=request_id,
                        ),
                        "subject": subject,
                        "gmail_thread_id": gmail_thread_id,
                        "source_email_id": source.get("id"),
                    }
                # Auto-replies alone must not resolve — already filtered in helper

                waiting_hours = waiting_duration_hours(source)
                if not threshold_elapsed(
                    priority=priority, waiting_hours=waiting_hours
                ):
                    return {
                        "_uncertain": True,
                        "subject": subject,
                        "gmail_thread_id": gmail_thread_id,
                        "source_email_id": source.get("id"),
                        "reason": (
                            f"Follow-up threshold not elapsed "
                            f"({waiting_hours:.1f}h waited)."
                        ),
                        "confidence": "medium",
                        "waiting_hours": waiting_hours,
                    }

                attempt_count = count_verified_followup_attempts(
                    messages,
                    account_email=account,
                    request_message_id=request_id,
                    message_direction_fn=message_direction,
                )
                last_outgoing_at = (
                    source.get("received_at")
                    or source.get("sent_at")
                    or ""
                )
                # Prefer external recipient display when available
                for msg in reversed(_sort_thread(messages)):
                    if message_direction(msg, account) != "incoming":
                        continue
                    if is_automatic_reply(msg):
                        continue
                    recipient = sender_display(msg.get("sender")) or recipient
                    break
            except Exception:
                logger.exception("Enhanced follow-up enrichment failed")

    item: dict[str, Any] = {
        "title": title,
        "description": str(
            analysis.get("summary")
            or source.get("snippet")
            or classified.get("reason")
            or ""
        )[:420],
        "priority": priority,
        "recommended_action": action,
        "kind": kind,
        "source_email_id": source.get("id"),
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "received_at": source.get("received_at") or "",
        "followup_state": state,
        "followup_confidence": evidence,
        "followup_reason": classified.get("reason"),
        "gmail_thread_id": gmail_thread_id,
        "waiting_hours": round(float(waiting_hours), 1) if waiting_hours else 0,
        "followup_attempt_count": int(attempt_count or 0),
        "last_outgoing_at": last_outgoing_at,
        "evidence_status": evidence,
    }

    if enhanced:
        try:
            from ai_followup_assistant import (
                followup_actions_enabled,
                stable_followup_key,
                threshold_hours_for_priority,
            )

            key = stable_followup_key(
                account_email=account,
                gmail_thread_id=str(gmail_thread_id or ""),
                request_message_id=request_id,
            )
            item["recommendation_key"] = key
            item["recommendation_id"] = key
            item["followup_actions_enabled"] = followup_actions_enabled()
            item["followup_threshold_hours"] = threshold_hours_for_priority(priority)
            item["next_action"] = "draft_followup"
        except Exception:
            logger.exception("Failed attaching follow-up recommendation key")

    return item


def detect_followups_for_emails(
    emails: list[dict] | None,
    *,
    account_email: str,
    gmail_account_email: str | None = None,
    list_thread_messages=None,
) -> list[dict[str, Any]]:
    """
    Detect pending_reply / awaiting_response suggestions for briefing emails.

    Groups by gmail_thread_id, loads full stored thread when helper provided.
    When AI_FOLLOWUP_ASSISTANT_ENABLED, applies thresholds and richer fields.
    """
    detected = detect_followups_for_account_sync(
        emails,
        account_email=account_email,
        gmail_account_email=gmail_account_email,
        list_thread_messages=list_thread_messages,
    )
    return list(detected.get("active") or [])


def detect_followups_for_account_sync(
    emails: list[dict] | None,
    *,
    account_email: str,
    gmail_account_email: str | None = None,
    list_thread_messages=None,
) -> dict[str, Any]:
    """
    Full detection pass returning active items, uncertain rows, and resolved keys.
    """
    account = (account_email or "").strip()
    empty = {"active": [], "uncertain": [], "resolved_keys": []}
    if not account:
        return empty

    enhanced = _enhanced_assistant_enabled()
    rows = [email for email in (emails or []) if isinstance(email, dict)]
    thread_map: dict[str, list[dict]] = {}
    for email in rows:
        tid = str(email.get("gmail_thread_id") or email.get("thread_id") or "").strip()
        if not tid:
            tid = f"msg:{email.get('id') or id(email)}"
        thread_map.setdefault(tid, []).append(email)

    account_key = (gmail_account_email or account).strip()
    cleared_threads = _sent_draft_thread_ids(account_key) if account_key else set()
    if list_thread_messages is None and account_key:
        try:
            from db import list_followup_threads

            preloaded = list_followup_threads(
                gmail_account_email=account_key,
                thread_ids=list(thread_map),
            )

            def list_thread_messages(*, gmail_account_email: str, thread_id: str):
                if gmail_account_email != account_key:
                    return []
                return list(preloaded.get(thread_id) or [])
        except Exception:
            logger.exception("Follow-up thread preload failed; loading per thread")

    results: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    resolved_keys: list[str] = []

    for thread_id, seed_messages in thread_map.items():
        messages = _load_thread_messages(
            account_key=account_key,
            thread_id=thread_id,
            seed_messages=seed_messages,
            list_thread_messages=list_thread_messages,
        )
        classified = classify_thread_state(
            messages,
            account_email=account,
            sent_cleared=thread_id in cleared_threads,
        )
        state = classified.get("state")

        if enhanced and state not in {"pending_reply", "awaiting_response"}:
            # Auto-resolve tracked awaiting follow-ups when a human reply arrived
            # (classify may return resolved/informational/unknown — not only resolved).
            try:
                from ai_followup_assistant import (
                    is_automatic_reply,
                    looks_like_outgoing_request,
                    message_epoch_ms,
                    relevant_incoming_response,
                    stable_followup_key,
                )

                sorted_msgs = _sort_thread(messages)
                last_out = None
                for msg in reversed(sorted_msgs):
                    if message_direction(msg, account) != "outgoing":
                        continue
                    if looks_like_outgoing_request(msg, _analysis(msg)):
                        last_out = msg
                        break
                if last_out is None:
                    last_out = next(
                        (
                            msg
                            for msg in reversed(sorted_msgs)
                            if message_direction(msg, account) == "outgoing"
                        ),
                        None,
                    )
                if last_out:
                    reply = relevant_incoming_response(
                        messages,
                        account_email=account,
                        after_ms=message_epoch_ms(last_out),
                        message_direction_fn=message_direction,
                    )
                    # Also accept classifier "resolved" (our outbound after inbound)
                    if reply is not None or state == "resolved":
                        if reply is None or not is_automatic_reply(reply):
                            resolved_keys.append(
                                stable_followup_key(
                                    account_email=account,
                                    gmail_thread_id=(
                                        thread_id
                                        if not thread_id.startswith("msg:")
                                        else str(last_out.get("gmail_thread_id") or "")
                                    ),
                                    request_message_id=str(last_out.get("id") or ""),
                                )
                            )
            except Exception:
                logger.exception(
                    "Failed computing resolved follow-up key thread_id=%s",
                    thread_id,
                )

        if state not in {"pending_reply", "awaiting_response"}:
            continue
        if (
            not enhanced
            and classified.get("confidence") == "low"
            and state == "awaiting_response"
        ):
            # Task 2: avoid noisy awaiting when SENT history incomplete
            continue

        source_id = classified.get("source_email_id")
        source = next(
            (
                msg
                for msg in _sort_thread(messages)
                if str(msg.get("id") or "") == str(source_id or "")
            ),
            _sort_thread(messages)[-1],
        )
        built = _build_followup_item(
            state=state,
            classified=classified,
            source=source,
            messages=messages,
            account=account,
            thread_id=thread_id,
            enhanced=enhanced,
        )
        if not built:
            continue
        if built.get("_uncertain"):
            uncertain.append(built)
            continue
        if built.get("_resolved"):
            key = built.get("recommendation_key")
            if key:
                resolved_keys.append(str(key))
            continue
        results.append(built)

    rank = {"pending_reply": 0, "awaiting_response": 1}
    conf_rank = {"high": 0, "medium": 1, "low": 2}
    results.sort(
        key=lambda row: (
            rank.get(str(row.get("followup_state")), 9),
            conf_rank.get(str(row.get("followup_confidence")), 9),
            str(row.get("subject") or ""),
        )
    )
    return {
        "active": results[:MAX_FOLLOWUPS],
        "uncertain": uncertain,
        "resolved_keys": list(dict.fromkeys(resolved_keys)),
    }


def apply_pending_replies_to_payload(
    payload: dict,
    emails: list[dict] | None,
    *,
    account_email: str,
    gmail_account_email: str | None = None,
    list_thread_messages=None,
) -> dict:
    """
    Merge follow-up suggestions into existing briefing fields (no new UI).

    Safe no-op when nothing detected.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    account_key = gmail_account_email or account_email
    detected = detect_followups_for_account_sync(
        emails,
        account_email=account_email,
        gmail_account_email=account_key,
        list_thread_messages=list_thread_messages,
    )
    followups = list(detected.get("active") or [])
    enhanced = _enhanced_assistant_enabled()

    if enhanced:
        try:
            from ai_followup_assistant import (
                followup_actions_enabled,
                suppressed_followup_keys,
                sync_followup_recommendation_states,
            )

            suppressed = suppressed_followup_keys(account_key)
            if suppressed:
                followups = [
                    item
                    for item in followups
                    if str(item.get("recommendation_key") or "") not in suppressed
                ]
            sync_followup_recommendation_states(
                account_email=account_key,
                followup_items=followups,
                resolved_keys=list(detected.get("resolved_keys") or []),
            )
            actions_on = followup_actions_enabled()
            for item in followups:
                item["followup_actions_enabled"] = actions_on
        except Exception:
            logger.exception("Enhanced follow-up state sync during briefing failed")

    existing_items = [
        item for item in (payload.get("items") or []) if isinstance(item, dict)
    ]
    followup_ids = {
        str(item.get("source_email_id"))
        for item in followups
        if item.get("source_email_id")
    }

    merged: list[dict] = list(followups)
    for item in existing_items:
        source_id = str(item.get("source_email_id") or "")
        if source_id and source_id in followup_ids:
            continue
        merged.append(item)
        if len(merged) >= MAX_BRIEFING_ITEMS:
            break
    merged = merged[:MAX_BRIEFING_ITEMS]

    meetings = [
        meeting
        for meeting in (payload.get("meetings") or [])
        if isinstance(meeting, dict)
    ]
    overview = build_overview(merged, meetings)

    if followups:
        labels = []
        for item in followups:
            state = item.get("followup_state")
            if state == "pending_reply":
                labels.append(f"reply: {item.get('subject')}")
            elif state == "awaiting_response":
                labels.append(f"awaiting: {item.get('subject')}")
        if labels:
            overview["text"] = (
                "Follow-ups: " + "; ".join(labels) + ". " + str(overview.get("text") or "")
            ).strip()

    payload = dict(payload)
    payload["items"] = merged
    payload["overview"] = overview
    payload["action_items"] = [
        item.get("recommended_action") or item.get("title")
        for item in merged
        if item.get("kind") in {"action", "followup", "pending_reply"}
        or item.get("recommended_action")
    ] or list(payload.get("action_items") or [])
    payload["highlights"] = [
        item.get("title")
        for item in merged
        if item.get("kind") not in {"action", "followup", "pending_reply"}
    ] or list(payload.get("highlights") or [])
    payload["pending_replies"] = {
        "enabled": True,
        "assistant_enabled": enhanced,
        "actions_enabled": bool(
            enhanced
            and any(item.get("followup_actions_enabled") for item in followups)
        ),
        "count": len(followups),
        "uncertain_count": len(detected.get("uncertain") or []),
        "items": [
            {
                "state": item.get("followup_state"),
                "confidence": item.get("followup_confidence"),
                "source_email_id": item.get("source_email_id"),
                "gmail_thread_id": item.get("gmail_thread_id"),
                "subject": item.get("subject"),
                "reason": item.get("followup_reason"),
                "recommendation_key": item.get("recommendation_key"),
                "waiting_hours": item.get("waiting_hours"),
                "attempt_count": item.get("followup_attempt_count"),
            }
            for item in followups
        ],
    }
    return payload
