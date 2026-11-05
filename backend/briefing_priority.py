"""Deterministic email priority scoring for Smart Daily Briefing 2.0.

Uses existing email_analyses fields and deadline parsing only.
Numerical scoring does not call OpenAI.

When SMART_BRIEFING_ENABLED=true, helpers reorder briefing payload fields
that the existing UI already renders (items, overview, action_items).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from typing import Any

from briefing_structure import _analysis, build_overview, parse_due_date, sender_display

logger = logging.getLogger(__name__)

SCORE_IMPORTANCE_HIGH = 40
SCORE_IMPORTANCE_MEDIUM = 20
SCORE_ACTION_REQUIRED = 25
SCORE_DEADLINE_OVERDUE = 25
SCORE_DEADLINE_TODAY = 20
SCORE_DEADLINE_TOMORROW = 10
SCORE_HIGH_CONFIDENCE = 10
CONFIDENCE_THRESHOLD = 0.85
SCORE_MIN = 0
SCORE_MAX = 100
TOP_PRIORITIES_LIMIT = 3
MAX_BRIEFING_ITEMS = 6


def smart_briefing_enabled() -> bool:
    return os.getenv("SMART_BRIEFING_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _as_confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf != conf:  # NaN
        return None
    return conf


def _deadline_texts(analysis: dict) -> list[str]:
    raw = analysis.get("deadlines")
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _best_deadline_bonus(
    deadline_texts: list[str], briefing_day: date
) -> tuple[int, str | None]:
    """Strongest deadline contribution: overdue > today > tomorrow."""
    best = 0
    label: str | None = None
    tomorrow = briefing_day + timedelta(days=1)

    for text in deadline_texts:
        due = parse_due_date(text)
        overdue_marked = "overdue" in text.lower()
        if due is None:
            if overdue_marked and SCORE_DEADLINE_OVERDUE > best:
                best = SCORE_DEADLINE_OVERDUE
                label = "overdue"
            continue

        if due < briefing_day or overdue_marked:
            if SCORE_DEADLINE_OVERDUE > best:
                best = SCORE_DEADLINE_OVERDUE
                label = "overdue"
        elif due == briefing_day:
            if SCORE_DEADLINE_TODAY > best:
                best = SCORE_DEADLINE_TODAY
                label = "today"
        elif due == tomorrow:
            if SCORE_DEADLINE_TOMORROW > best:
                best = SCORE_DEADLINE_TOMORROW
                label = "tomorrow"

    return best, label


def score_email_priority(
    email: dict | None,
    *,
    briefing_day: date,
    analysis: dict | None = None,
    vip_emails: set[str] | None = None,
    feedback_delta: int = 0,
) -> dict[str, Any]:
    """Deterministic 0–100 priority score for one stored email.

    Optional VIP / feedback adjustments (Tasks 5–6) are bounded add-ons to the
    same score — not a second ranking system. Defaults keep classic behavior.
    """
    row = email if isinstance(email, dict) else {}
    analysis_row = analysis if isinstance(analysis, dict) else _analysis(row)
    if not isinstance(analysis_row, dict):
        analysis_row = {}

    importance_raw = analysis_row.get("importance")
    importance = (
        str(importance_raw).strip().lower() if importance_raw is not None else ""
    )
    if importance not in {"high", "medium", "low"}:
        importance = "unknown"

    action_required = _as_bool(analysis_row.get("action_required"))
    confidence = _as_confidence(analysis_row.get("confidence"))
    deadline_texts = _deadline_texts(analysis_row)
    deadline_bonus, deadline_bucket = _best_deadline_bonus(
        deadline_texts, briefing_day
    )

    breakdown: dict[str, int] = {
        "importance": 0,
        "action_required": 0,
        "deadline": 0,
        "confidence": 0,
        "vip": 0,
        "feedback": 0,
    }

    if importance == "high":
        breakdown["importance"] = SCORE_IMPORTANCE_HIGH
    elif importance == "medium":
        breakdown["importance"] = SCORE_IMPORTANCE_MEDIUM

    if action_required:
        breakdown["action_required"] = SCORE_ACTION_REQUIRED

    breakdown["deadline"] = deadline_bonus

    if confidence is not None and confidence >= CONFIDENCE_THRESHOLD:
        breakdown["confidence"] = SCORE_HIGH_CONFIDENCE

    base_score = max(SCORE_MIN, min(SCORE_MAX, sum(
        v for k, v in breakdown.items() if k not in {"vip", "feedback"}
    )))

    vip_bonus = 0
    feedback_applied = 0
    try:
        from briefing_feedback import apply_feedback_adjustment, apply_vip_bonus
        from briefing_preferences import sender_is_vip

        is_vip = sender_is_vip(row, vip_emails or set())
        score_after_vip, vip_bonus = apply_vip_bonus(base_score, is_vip=is_vip)
        breakdown["vip"] = vip_bonus
        urgent = bool(
            action_required
            or deadline_bucket in {"overdue", "today"}
            or importance == "high"
        )
        score, feedback_applied = apply_feedback_adjustment(
            score_after_vip,
            feedback_delta=int(feedback_delta or 0),
            urgent=urgent,
        )
        breakdown["feedback"] = feedback_applied
    except Exception:
        score = base_score

    score = max(SCORE_MIN, min(SCORE_MAX, int(score)))

    return {
        "score": score,
        "breakdown": breakdown,
        "importance": importance if importance != "unknown" else None,
        "action_required": action_required,
        "confidence": confidence,
        "deadline_bucket": deadline_bucket,
        "deadline_texts": deadline_texts,
        "briefing_day": briefing_day.isoformat(),
        "email_id": row.get("id"),
        "gmail_thread_id": row.get("gmail_thread_id") or row.get("thread_id"),
        "subject": str(row.get("subject") or "").strip(),
        "sender": sender_display(row.get("sender")),
        "summary": str(
            analysis_row.get("summary") or row.get("snippet") or ""
        ).strip(),
    }


def score_emails_priority(
    emails: list[dict] | None,
    *,
    briefing_day: date,
) -> list[dict[str, Any]]:
    """Score many emails; stable order matching input order."""
    rows = emails if isinstance(emails, list) else []
    return [
        score_email_priority(email, briefing_day=briefing_day)
        for email in rows
        if isinstance(email, dict)
    ]


def is_actionable_priority(scored: dict[str, Any]) -> bool:
    """True when the email warrants a daily priority slot."""
    if scored.get("action_required"):
        return True
    if scored.get("deadline_bucket") in {"overdue", "today", "tomorrow"}:
        return True
    if scored.get("importance") == "high" and int(scored.get("score") or 0) > 0:
        return True
    return False


def build_priority_reason(scored: dict[str, Any]) -> str:
    """Concise, factual reason from scoring signals only."""
    parts: list[str] = []
    importance = scored.get("importance")
    if importance == "high":
        parts.append("marked high importance")
    elif importance == "medium":
        parts.append("marked medium importance")

    if scored.get("action_required"):
        parts.append("action required")

    bucket = scored.get("deadline_bucket")
    if bucket == "overdue":
        parts.append("deadline overdue")
    elif bucket == "today":
        parts.append("deadline today")
    elif bucket == "tomorrow":
        parts.append("deadline tomorrow")

    conf = scored.get("confidence")
    if isinstance(conf, (int, float)) and conf >= CONFIDENCE_THRESHOLD:
        parts.append("high analysis confidence")

    if not parts:
        return "Relevant based on stored email analysis."
    return "Priority because " + ", ".join(parts) + "."


def _thread_key(email: dict, scored: dict[str, Any]) -> str:
    thread = scored.get("gmail_thread_id") or email.get("gmail_thread_id") or email.get(
        "thread_id"
    )
    if thread:
        return f"thread:{thread}"
    email_id = scored.get("email_id") or email.get("id")
    if email_id:
        return f"email:{email_id}"
    subject = re.sub(r"\s+", " ", str(email.get("subject") or "").strip().lower())
    return f"subject:{subject}" if subject else f"anon:{id(email)}"


def _ranking_context(
    *,
    account_email: str | None,
) -> tuple[set[str], dict[str, Any]]:
    """Load VIP set + feedback signals when personalization/adaptive flags allow."""
    vip: set[str] = set()
    signals: dict[str, Any] = {"by_key": {}, "domain_deltas": {}, "type_counts": {}}
    if not account_email:
        return vip, signals
    try:
        from briefing_preferences import (
            get_preferences_for_account,
            personalization_enabled,
            vip_contact_set,
        )

        if personalization_enabled():
            prefs = get_preferences_for_account(account_email).get("preferences") or {}
            vip = vip_contact_set(prefs)
    except Exception:
        logger.exception("VIP preference load failed; continuing without VIP boost")

    try:
        from briefing_feedback import (
            adaptive_ranking_enabled,
            aggregate_feedback_signals,
            feedback_enabled,
        )
        from db import list_recommendation_feedback_for_account

        if adaptive_ranking_enabled() and feedback_enabled():
            rows = list_recommendation_feedback_for_account(
                gmail_account_email=account_email
            )
            signals = aggregate_feedback_signals(rows)
    except Exception:
        logger.exception("Feedback signal load failed; continuing without deltas")
    return vip, signals


def select_top_priorities(
    emails: list[dict] | None,
    *,
    briefing_day: date,
    limit: int = TOP_PRIORITIES_LIMIT,
    account_email: str | None = None,
) -> list[dict[str, Any]]:
    """
    Rank actionable emails by score; one per thread; max `limit`.

    Never invents items — only returns emails present in the input list.
    """
    rows = [email for email in (emails or []) if isinstance(email, dict)]
    vip_emails, signals = _ranking_context(account_email=account_email)
    ranked: list[tuple[dict[str, Any], dict]] = []
    for email in rows:
        feedback_delta = 0
        try:
            from briefing_feedback import feedback_delta_for_email
            from briefing_preferences import extract_sender_email

            feedback_delta = feedback_delta_for_email(
                signals=signals,
                sender_email=extract_sender_email(email),
            )
        except Exception:
            feedback_delta = 0
        scored = score_email_priority(
            email,
            briefing_day=briefing_day,
            vip_emails=vip_emails,
            feedback_delta=feedback_delta,
        )
        if not is_actionable_priority(scored):
            continue
        ranked.append((scored, email))

    ranked.sort(
        key=lambda pair: (
            -int(pair[0].get("score") or 0),
            str(pair[0].get("email_id") or ""),
        )
    )

    selected: list[dict[str, Any]] = []
    seen_threads: set[str] = set()
    for scored, email in ranked:
        key = _thread_key(email, scored)
        if key in seen_threads:
            continue
        seen_threads.add(key)
        reason = build_priority_reason(scored)
        importance = scored.get("importance") or "medium"
        if importance not in {"high", "medium", "low"}:
            importance = "medium"
        subject = scored.get("subject") or "Email update"
        summary = scored.get("summary") or subject
        selected.append(
            {
                "title": subject[:120],
                "description": (summary[:420] if summary else subject)[:420],
                "priority": importance,
                "recommended_action": reason,
                "kind": "action" if scored.get("action_required") else "update",
                "source_email_id": email.get("id"),
                "sender": scored.get("sender") or sender_display(email.get("sender")),
                "subject": subject,
                "received_at": email.get("received_at") or "",
                "priority_score": int(scored.get("score") or 0),
                "priority_reason": reason,
                "gmail_thread_id": scored.get("gmail_thread_id"),
            }
        )
        if len(selected) >= max(0, limit):
            break
    return selected


def order_emails_by_priority(
    emails: list[dict] | None,
    *,
    briefing_day: date,
    account_email: str | None = None,
) -> list[dict]:
    """Stable descending score order for LLM context (no drops)."""
    rows = [email for email in (emails or []) if isinstance(email, dict)]
    vip_emails, signals = _ranking_context(account_email=account_email)

    def _score_one(email: dict) -> int:
        feedback_delta = 0
        try:
            from briefing_feedback import feedback_delta_for_email
            from briefing_preferences import extract_sender_email

            feedback_delta = feedback_delta_for_email(
                signals=signals,
                sender_email=extract_sender_email(email),
            )
        except Exception:
            feedback_delta = 0
        return score_email_priority(
            email,
            briefing_day=briefing_day,
            vip_emails=vip_emails,
            feedback_delta=feedback_delta,
        )["score"]

    decorated = [
        (_score_one(email), index, email) for index, email in enumerate(rows)
    ]
    decorated.sort(key=lambda row: (-row[0], row[1]))
    return [email for _score, _index, email in decorated]


def apply_smart_priorities_to_payload(
    payload: dict,
    emails: list[dict] | None,
    *,
    briefing_day: date,
    account_email: str | None = None,
) -> dict:
    """
    Merge top priorities into existing briefing fields the UI already shows.

    Does not add new UI sections. Optional `priority_score` on items is ignored
    by current React types but stored in JSONB for compatibility.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    top = select_top_priorities(
        emails, briefing_day=briefing_day, account_email=account_email
    )
    existing_items = [
        item for item in (payload.get("items") or []) if isinstance(item, dict)
    ]
    top_ids = {
        str(item.get("source_email_id"))
        for item in top
        if item.get("source_email_id")
    }

    merged: list[dict] = list(top)
    for item in existing_items:
        source_id = str(item.get("source_email_id") or "")
        if source_id and source_id in top_ids:
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
    overview = build_overview(merged, meetings, briefing_day=briefing_day)
    if top:
        lead = "; ".join(
            f"{item.get('title')} ({item.get('priority_score', 0)})"
            for item in top[:TOP_PRIORITIES_LIMIT]
        )
        overview["text"] = (
            f"Top priorities: {lead}. " + str(overview.get("text") or "")
        ).strip()

    payload = dict(payload)
    payload["items"] = merged
    payload["overview"] = overview
    payload["highlights"] = [
        item.get("title") for item in merged if item.get("kind") != "action"
    ] or list(payload.get("highlights") or [])
    payload["action_items"] = [
        item.get("recommended_action") or item.get("title")
        for item in merged
        if item.get("kind") == "action" or item.get("recommended_action")
    ]
    # Optional metadata for API consumers; ignored by current UI.
    payload["smart_briefing"] = {
        "enabled": True,
        "top_priority_count": len(top),
        "top_priority_email_ids": [
            str(item.get("source_email_id"))
            for item in top
            if item.get("source_email_id")
        ],
    }
    return payload


def ranking_context_for_prompt(
    emails: list[dict] | None,
    *,
    briefing_day: date,
) -> str:
    """Short ranking block for the existing generate_briefing_content prompt."""
    top = select_top_priorities(emails, briefing_day=briefing_day)
    if not top:
        return (
            "Smart ranking: no actionable high-priority emails identified "
            "from analysis. Prefer real high-importance or action_required "
            "items only; never invent priorities."
        )
    lines = [
        "Smart ranking (deterministic scores; use these as the leading items):"
    ]
    for index, item in enumerate(top, start=1):
        lines.append(
            f"{index}. score={item.get('priority_score')} "
            f"priority={item.get('priority')} "
            f"email_id={item.get('source_email_id')} "
            f"subject={item.get('subject')!r} "
            f"reason={item.get('priority_reason')}"
        )
    lines.append(
        "Ground every recommendation in these emails. Do not invent dates, "
        "meetings, or commitments."
    )
    return "\n".join(lines)
