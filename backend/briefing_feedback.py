"""Task 6 — Recommendation feedback, adaptive ranking, validation, metrics.

Feature flags:
  BRIEFING_FEEDBACK_ENABLED (default false)
  BRIEFING_ADAPTIVE_RANKING_ENABLED (default false)

Uses explicit feedback only. No model fine-tuning. Bounded score deltas.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

FEEDBACK_TYPES = frozenset(
    {
        "useful",
        "not_useful",
        "incorrect_priority",
        "already_handled",
        "irrelevant",
    }
)

# Bounded adaptive adjustments (deterministic).
DELTA_USEFUL = 8
DELTA_NOT_USEFUL = -6
DELTA_INCORRECT_PRIORITY = -5
DELTA_ALREADY_HANDLED = -4
DELTA_IRRELEVANT = -10
DELTA_VIP = 15
ABS_FEEDBACK_CAP = 12
SCORE_MIN = 0
SCORE_MAX = 100

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


def feedback_enabled() -> bool:
    return os.getenv("BRIEFING_FEEDBACK_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def adaptive_ranking_enabled() -> bool:
    return os.getenv("BRIEFING_ADAPTIVE_RANKING_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def clamp_score(value: int | float) -> int:
    return max(SCORE_MIN, min(SCORE_MAX, int(value)))


def feedback_delta_for_type(feedback_type: str) -> int:
    mapping = {
        "useful": DELTA_USEFUL,
        "not_useful": DELTA_NOT_USEFUL,
        "incorrect_priority": DELTA_INCORRECT_PRIORITY,
        "already_handled": DELTA_ALREADY_HANDLED,
        "irrelevant": DELTA_IRRELEVANT,
    }
    return mapping.get(feedback_type, 0)


def bound_feedback_delta(delta: int) -> int:
    return max(-ABS_FEEDBACK_CAP, min(ABS_FEEDBACK_CAP, int(delta)))


def apply_vip_bonus(score: int, *, is_vip: bool) -> tuple[int, int]:
    """Return (new_score, vip_bonus_applied)."""
    if not is_vip:
        return clamp_score(score), 0
    bonus = DELTA_VIP
    return clamp_score(score + bonus), bonus


def apply_feedback_adjustment(
    score: int,
    *,
    feedback_delta: int,
    urgent: bool = False,
) -> tuple[int, int]:
    """
    Apply bounded feedback delta. Urgent items cannot be driven below a floor
    that would suppress them entirely from actionable consideration.
    """
    delta = bound_feedback_delta(feedback_delta)
    adjusted = clamp_score(score + delta)
    if urgent and delta < 0:
        # Keep urgent mail visible: floor at 40 when base was actionable/urgent.
        adjusted = max(adjusted, 40)
        delta = adjusted - clamp_score(score)
    return adjusted, delta


def sender_domain(email_addr: str | None) -> str | None:
    if not email_addr or "@" not in email_addr:
        return None
    return email_addr.rsplit("@", 1)[-1].lower().strip() or None


def aggregate_feedback_signals(
    feedback_rows: list[dict] | None,
) -> dict[str, Any]:
    """
    Build deterministic lookup maps from stored feedback.
    Missing feedback is never treated as negative.
    """
    by_key: dict[str, str] = {}
    domain_scores: dict[str, int] = {}
    type_counts: dict[str, int] = {t: 0 for t in FEEDBACK_TYPES}

    for row in feedback_rows or []:
        if not isinstance(row, dict):
            continue
        ftype = str(row.get("feedback_type") or "")
        if ftype not in FEEDBACK_TYPES:
            continue
        key = str(row.get("recommendation_key") or "")
        if key:
            by_key[key] = ftype
        type_counts[ftype] = type_counts.get(ftype, 0) + 1
        meta = row.get("metadata") or {}
        domain = None
        if isinstance(meta, dict):
            domain = sender_domain(str(meta.get("sender_email") or ""))
        if domain:
            domain_scores[domain] = domain_scores.get(domain, 0) + feedback_delta_for_type(
                ftype
            )

    # Bound domain aggregates
    for domain, value in list(domain_scores.items()):
        domain_scores[domain] = bound_feedback_delta(value)

    return {
        "by_key": by_key,
        "domain_deltas": domain_scores,
        "type_counts": type_counts,
    }


def feedback_delta_for_email(
    *,
    signals: dict[str, Any],
    recommendation_key: str | None = None,
    sender_email: str | None = None,
) -> int:
    if not adaptive_ranking_enabled():
        return 0
    delta = 0
    by_key = signals.get("by_key") or {}
    if recommendation_key and recommendation_key in by_key:
        delta += feedback_delta_for_type(by_key[recommendation_key])
    domain = sender_domain(sender_email)
    domain_deltas = signals.get("domain_deltas") or {}
    if domain and domain in domain_deltas:
        # Modest domain influence (half weight, already bounded overall)
        delta += int(domain_deltas[domain] / 2)
    return bound_feedback_delta(delta)


def submit_recommendation_feedback(
    *,
    account_email: str,
    recommendation_key: str,
    feedback_type: str,
    metadata: dict | None = None,
) -> dict[str, Any]:
    if not feedback_enabled():
        raise PermissionError("feedback_disabled")
    ftype = (feedback_type or "").strip().lower()
    if ftype not in FEEDBACK_TYPES:
        raise ValueError(
            "feedback_type must be one of: " + ", ".join(sorted(FEEDBACK_TYPES))
        )
    key = (recommendation_key or "").strip()
    if not key:
        raise ValueError("recommendation_key is required")

    # Ownership: recommendation must resolve for this account when interactive data exists
    owned = None
    try:
        from briefing_recommendations import get_owned_recommendation

        owned = get_owned_recommendation(
            account_email=account_email, recommendation_key=key
        )
    except Exception:
        logger.exception("Could not resolve recommendation ownership")

    if owned is None:
        # Still allow feedback keyed to a known prior recommendation_key in state table
        from db import get_recommendation_state

        state = get_recommendation_state(
            gmail_account_email=account_email,
            recommendation_key=key,
        )
        if not state:
            raise KeyError("recommendation_not_found")
    else:
        key = str(owned.get("recommendation_key") or key)

    safe_meta = {}
    if isinstance(metadata, dict):
        for field in ("sender_email", "source_email_id", "gmail_thread_id", "title"):
            if field in metadata and metadata[field] is not None:
                safe_meta[field] = str(metadata[field])[:200]
    if owned:
        if owned.get("source_email_id"):
            safe_meta.setdefault("source_email_id", owned.get("source_email_id"))
        if owned.get("title"):
            safe_meta.setdefault("title", str(owned.get("title"))[:200])

    from db import upsert_recommendation_feedback

    row = upsert_recommendation_feedback(
        gmail_account_email=account_email,
        recommendation_key=key,
        feedback_type=ftype,
        metadata=safe_meta,
    )
    return {
        "feedback": {
            "id": row.get("id"),
            "recommendation_key": key,
            "feedback_type": row.get("feedback_type"),
            "updated_at": row.get("updated_at"),
        }
    }


def feedback_summary_for_account(account_email: str) -> dict[str, Any]:
    from db import (
        list_recommendation_feedback_for_account,
        list_recommendation_states_for_account,
        get_quality_counters,
    )

    feedback_rows = list_recommendation_feedback_for_account(
        gmail_account_email=account_email
    )
    states = list_recommendation_states_for_account(
        gmail_account_email=account_email
    )
    signals = aggregate_feedback_signals(feedback_rows)
    status_counts = {
        "pending": 0,
        "completed": 0,
        "snoozed": 0,
        "dismissed": 0,
    }
    for row in states:
        status = str(row.get("status") or "")
        if status in status_counts:
            status_counts[status] += 1

    today = date.today().isoformat()
    counters = get_quality_counters(
        gmail_account_email=account_email, metric_date=today
    )
    metrics = (counters or {}).get("metrics") if counters else {}

    return {
        "enabled": feedback_enabled(),
        "adaptive_ranking_enabled": adaptive_ranking_enabled(),
        "feedback_counts": signals["type_counts"],
        "recommendation_status_counts": status_counts,
        "quality": {
            "recommendations_generated": int(
                (metrics or {}).get("recommendations_generated") or 0
            ),
            "duplicates_prevented": int(
                (metrics or {}).get("duplicates_prevented") or 0
            ),
            "validation_failures": int(
                (metrics or {}).get("validation_failures") or 0
            ),
            "metric_date": today,
        },
        "note": (
            "Completion means the user acknowledged a recommendation; "
            "it is not verified email-task completion. Missing feedback "
            "is not treated as negative."
        ),
    }


def increment_quality_metric(
    *,
    account_email: str,
    metric: str,
    amount: int = 1,
    metric_date: date | None = None,
) -> None:
    if not account_email or amount == 0:
        return
    day = (metric_date or date.today()).isoformat()
    try:
        from db import bump_quality_counter

        bump_quality_counter(
            gmail_account_email=account_email,
            metric_date=day,
            metric=metric,
            amount=amount,
        )
    except Exception:
        logger.exception("Failed bumping quality metric=%s", metric)


def validate_recommendation_candidate(
    candidate: dict[str, Any],
    *,
    account_email: str,
    known_email_ids: set[str] | None = None,
) -> tuple[bool, str | None]:
    """
    Pre-save recommendation quality checks.
    Returns (ok, failure_reason).
    """
    if not isinstance(candidate, dict):
        return False, "not_an_object"

    source_email_id = candidate.get("source_email_id")
    if source_email_id:
        sid = str(source_email_id).strip()
        if not UUID_RE.match(sid):
            return False, "invalid_source_email_id"
        if known_email_ids is not None and sid not in known_email_ids:
            return False, "source_email_missing"

    score = candidate.get("priority_score")
    if score is not None:
        try:
            value = int(score)
        except (TypeError, ValueError):
            return False, "priority_score_invalid"
        if value < SCORE_MIN or value > SCORE_MAX:
            return False, "priority_score_out_of_range"

    # Deadline items should have due evidence when claiming a due_date
    if candidate.get("due_date") and not (
        candidate.get("source_email_id") or candidate.get("deadline_texts")
    ):
        return False, "deadline_unsupported"

    title = str(candidate.get("title") or "").strip()
    if not title:
        return False, "missing_title"

    return True, None


def filter_validated_recommendations(
    candidates: list[dict],
    *,
    account_email: str,
    known_email_ids: set[str] | None = None,
    seen_keys: set[str] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Validate + dedupe by recommendation_key / thread+type."""
    from briefing_recommendations import _stable_key

    stats = {"kept": 0, "validation_failures": 0, "duplicates_prevented": 0}
    out: list[dict] = []
    seen = set(seen_keys or [])

    for cand in candidates or []:
        if not isinstance(cand, dict):
            stats["validation_failures"] += 1
            continue
        ok, reason = validate_recommendation_candidate(
            cand,
            account_email=account_email,
            known_email_ids=known_email_ids,
        )
        if not ok:
            stats["validation_failures"] += 1
            logger.info(
                "Recommendation validation failed reason=%s title=%r",
                reason,
                str(cand.get("title") or "")[:80],
            )
            continue

        key = str(cand.get("recommendation_key") or "").strip()
        if not key:
            key = _stable_key(
                account_email,
                str(cand.get("source_type") or "briefing_item"),
                str(cand.get("source_email_id") or cand.get("gmail_thread_id") or ""),
                str(cand.get("title") or ""),
                str(cand.get("kind") or "action"),
            )
            cand = dict(cand)
            cand["recommendation_key"] = key

        if key in seen:
            stats["duplicates_prevented"] += 1
            continue
        seen.add(key)
        out.append(cand)
        stats["kept"] += 1

    if account_email:
        if stats["validation_failures"]:
            increment_quality_metric(
                account_email=account_email,
                metric="validation_failures",
                amount=stats["validation_failures"],
            )
        if stats["duplicates_prevented"]:
            increment_quality_metric(
                account_email=account_email,
                metric="duplicates_prevented",
                amount=stats["duplicates_prevented"],
            )
        if stats["kept"]:
            increment_quality_metric(
                account_email=account_email,
                metric="recommendations_generated",
                amount=stats["kept"],
            )
    return out, stats


def suppressed_recommendation_keys(account_email: str) -> set[str]:
    """Keys completed/dismissed/irrelevant should not reappear without new evidence."""
    from db import list_recommendation_feedback_for_account, list_recommendation_states_for_account

    keys: set[str] = set()
    try:
        for row in list_recommendation_states_for_account(
            gmail_account_email=account_email
        ):
            if str(row.get("status") or "") in {"completed", "dismissed"}:
                keys.add(str(row.get("recommendation_key") or ""))
        if feedback_enabled():
            for row in list_recommendation_feedback_for_account(
                gmail_account_email=account_email
            ):
                if str(row.get("feedback_type") or "") in {
                    "irrelevant",
                    "already_handled",
                }:
                    keys.add(str(row.get("recommendation_key") or ""))
    except Exception:
        logger.exception("Failed loading suppressed recommendation keys")
    keys.discard("")
    return keys
