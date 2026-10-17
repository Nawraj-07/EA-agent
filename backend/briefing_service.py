"""Daily email briefing generation (Task 6.2)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from db import list_emails_for_briefing, list_stored_emails, save_briefing

logger = logging.getLogger(__name__)


class BriefingContent(BaseModel):
    headline: str = Field(..., min_length=1)
    highlights: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description="Short note when inbox is quiet or context is limited",
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _analysis_from_email(email: dict) -> dict:
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return analyses[0] if analyses else {}
    return analyses if isinstance(analyses, dict) else {}


def _build_context(emails: list[dict]) -> str:
    parts: list[str] = []
    for index, email in enumerate(emails, start=1):
        analysis = _analysis_from_email(email)
        deadlines = analysis.get("deadlines") or []
        parts.append(
            f"""
Email {index}
Subject: {email.get("subject") or ""}
Sender: {email.get("sender") or ""}
Date: {email.get("received_at") or ""}
Importance: {analysis.get("importance") or "unknown"}
Category: {analysis.get("category") or "unknown"}
Action required: {analysis.get("action_required")}
Summary: {analysis.get("summary") or ""}
Deadlines: {deadlines}
Snippet: {(email.get("snippet") or "")[:400]}
"""
        )
    return "\n---\n".join(parts)


def generate_briefing_content(emails: list[dict]) -> BriefingContent:
    if not emails:
        return BriefingContent(
            headline="Quiet inbox",
            highlights=[],
            deadlines=[],
            action_items=[],
            notes="No saved emails in the briefing window.",
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing")

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=0,
    ).with_structured_output(BriefingContent)

    context = _build_context(emails)
    result = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You create a concise daily executive briefing from email "
                    "summaries. Use only the provided context. Do not invent "
                    "deadlines, senders, or actions. Prefer high-importance and "
                    "action_required items. Keep lists short (max 5 items each)."
                )
            ),
            HumanMessage(
                content=(
                    f"Create today's briefing from these emails:\n\n{context}"
                )
            ),
        ]
    )
    return result


def run_daily_briefing() -> dict:
    """
    Build and store a daily briefing from recent stored emails.

    Safe on empty inbox / missing OpenAI: logs and returns without crashing
    the scheduler.

    If no emails fall inside BRIEFING_LOOKBACK_HOURS, falls back to the newest
    stored emails so a first-run / quiet-window still produces a briefing row.
    """
    lookback_hours = _env_int("BRIEFING_LOOKBACK_HOURS", 24)
    max_emails = _env_int("BRIEFING_MAX_EMAILS", 30)
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=lookback_hours)

    stats = {
        "email_count": 0,
        "saved": False,
        "briefing_id": None,
        "used_fallback": False,
        "status": "started",
    }

    try:
        emails = list_emails_for_briefing(
            since_iso=period_start.isoformat(),
            limit=max_emails,
        )
    except Exception:
        logger.exception(
            "Daily briefing failed while loading emails "
            "(status=load_failed lookback_hours=%s)",
            lookback_hours,
        )
        stats["status"] = "load_failed"
        return stats

    if not emails:
        logger.info(
            "Daily briefing: no emails in lookback window "
            "(lookback_hours=%s since=%s); falling back to newest stored emails",
            lookback_hours,
            period_start.isoformat(),
        )
        try:
            emails = list_stored_emails(limit=max_emails) or []
            stats["used_fallback"] = True
        except Exception:
            logger.exception(
                "Daily briefing fallback list_stored_emails failed "
                "(status=load_failed)"
            )
            stats["status"] = "load_failed"
            return stats

    stats["email_count"] = len(emails)
    logger.info(
        "Daily briefing context ready email_count=%s used_fallback=%s",
        stats["email_count"],
        stats["used_fallback"],
    )

    try:
        content = generate_briefing_content(emails)
    except Exception:
        logger.exception(
            "Daily briefing AI generation failed (status=generation_failed "
            "email_count=%s)",
            stats["email_count"],
        )
        stats["status"] = "generation_failed"
        return stats

    if stats["used_fallback"] and emails:
        fallback_note = (
            f"No emails were saved in the last {lookback_hours}h; "
            "this briefing uses your newest stored emails instead."
        )
        content.notes = (
            f"{fallback_note} {content.notes}".strip()
            if content.notes
            else fallback_note
        )
    elif not emails:
        content.notes = (
            content.notes
            or "No saved emails available yet. Analyze or sync mail first."
        )

    payload = {
        "headline": content.headline,
        "highlights": content.highlights,
        "deadlines": content.deadlines,
        "action_items": content.action_items,
        "notes": content.notes,
        "email_count": len(emails),
    }

    try:
        row = save_briefing(
            content=payload,
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
            email_count=len(emails),
        )
        stats["saved"] = True
        stats["briefing_id"] = row.get("id")
        stats["status"] = "saved"
        logger.info(
            "Daily briefing saved id=%s email_count=%s used_fallback=%s",
            stats["briefing_id"],
            stats["email_count"],
            stats["used_fallback"],
        )
    except Exception:
        logger.exception(
            "Daily briefing failed while saving to database "
            "(status=save_failed)"
        )
        stats["status"] = "save_failed"

    return stats
