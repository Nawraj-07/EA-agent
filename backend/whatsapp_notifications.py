"""Orchestrate WhatsApp outbox enqueue + send without affecting core Buddy flows.

All public notify_* helpers catch exceptions and never raise.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from whatsapp_service import (
    get_template_status,
    mask_recipient,
    normalize_whatsapp_number,
    send_template,
    template_is_sendable,
    truncate_param,
    whatsapp_enabled,
    whatsapp_template_language,
    whatsapp_test_recipient,
)

logger = logging.getLogger(__name__)

TYPE_DAILY = "daily_briefing"
TYPE_IMPORTANT = "important_email"
TYPE_MEETING = "meeting_reminder"
TYPE_PAYMENT = "payment_alert"
TYPE_TEST = "test"


def _template_name(env_key: str, default: str = "") -> str:
    return (os.getenv(env_key) or default).strip()


def _recipient() -> str:
    return whatsapp_test_recipient()


def _language() -> str:
    return whatsapp_template_language()


def _dispatch(
    *,
    notification_type: str,
    source_id: str,
    template_name: str,
    body_params: list[str] | None = None,
    payload_summary: dict | None = None,
    require_approved: bool = True,
) -> dict[str, Any]:
    """Claim outbox row (dedupe) then attempt Meta send. Never raises."""
    result: dict[str, Any] = {
        "ok": False,
        "status": "skipped",
        "outbox_id": None,
        "meta_message_id": None,
        "notification_type": notification_type,
        "template_name": template_name,
    }
    try:
        if not whatsapp_enabled():
            result["status"] = "disabled"
            return result

        recipient = _recipient()
        if not recipient:
            result["status"] = "invalid_recipient"
            result["error_message"] = "WHATSAPP_TEST_RECIPIENT is not set"
            return result

        if not template_name:
            result["status"] = "skipped"
            result["error_message"] = "Template name not configured"
            return result

        if require_approved and not template_is_sendable(template_name):
            info = get_template_status(template_name)
            result["status"] = "pending_approval"
            result["error_code"] = info.get("status") or "pending_approval"
            result["error_message"] = (
                f"Template {template_name} is not approved or is missing."
            )
            logger.info(
                "WhatsApp skipped pending template type=%s template=%s status=%s",
                notification_type,
                template_name,
                info.get("status"),
            )
            return result

        from db import claim_whatsapp_outbox_row, update_whatsapp_outbox_status

        row = claim_whatsapp_outbox_row(
            notification_type=notification_type,
            recipient=recipient,
            source_id=source_id,
            template_name=template_name,
            template_language=_language(),
            payload_summary=payload_summary or {},
        )
        if row is None:
            result["status"] = "duplicate"
            result["ok"] = True
            logger.info(
                "WhatsApp duplicate skipped type=%s source_id=%s recipient=%s",
                notification_type,
                source_id,
                mask_recipient(recipient),
            )
            return result

        outbox_id = row.get("id")
        result["outbox_id"] = outbox_id
        send_result = send_template(
            to=recipient,
            template_name=template_name,
            language_code=_language(),
            body_params=body_params or [],
        )
        status = send_result.get("status") or "failed"
        try:
            update_whatsapp_outbox_status(
                str(outbox_id),
                status=status if status != "accepted" else "accepted",
                meta_message_id=send_result.get("meta_message_id"),
                error_code=send_result.get("error_code"),
                error_message=send_result.get("error_message"),
                increment_retry=not send_result.get("ok"),
            )
        except Exception:
            logger.exception(
                "WhatsApp outbox status update failed outbox_id=%s", outbox_id
            )

        result["ok"] = bool(send_result.get("ok"))
        result["status"] = status
        result["meta_message_id"] = send_result.get("meta_message_id")
        result["error_code"] = send_result.get("error_code")
        result["error_message"] = send_result.get("error_message")
        return result
    except Exception:
        logger.exception(
            "WhatsApp notify failed type=%s source_id=%s",
            notification_type,
            source_id,
        )
        result["status"] = "failed"
        result["error_message"] = "WhatsApp notify failed; see backend logs."
        return result


def notify_whatsapp_daily_briefing(
    stats: dict[str, Any] | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """After successful briefing save. Never raises; never invents counts."""
    try:
        stats = stats or {}
        payload = payload or {}
        if not stats.get("saved") or not stats.get("briefing_id"):
            return {"ok": False, "status": "skipped", "error_message": "briefing not saved"}

        overview = payload.get("overview") if isinstance(payload.get("overview"), dict) else {}
        email_count = payload.get("email_count")
        if email_count is None:
            email_count = stats.get("email_count")
        try:
            email_count_i = int(email_count or 0)
        except (TypeError, ValueError):
            email_count_i = 0

        important = overview.get("important_updates")
        pending = overview.get("pending_actions")
        meetings = overview.get("meetings")
        try:
            important_i = int(important) if important is not None else 0
        except (TypeError, ValueError):
            important_i = 0
        try:
            pending_i = int(pending) if pending is not None else 0
        except (TypeError, ValueError):
            pending_i = 0
        try:
            meetings_i = int(meetings) if meetings is not None else 0
        except (TypeError, ValueError):
            meetings_i = 0

        if email_count_i == 0 and important_i == 0 and pending_i == 0 and meetings_i == 0:
            logger.info(
                "WhatsApp daily briefing skipped_empty briefing_id=%s",
                stats.get("briefing_id"),
            )
            return {"ok": True, "status": "skipped_empty"}

        template = _template_name(
            "WHATSAPP_TEMPLATE_DAILY_BRIEFING", "buddy_daily_briefing"
        )
        source_id = str(stats.get("briefing_id"))
        return _dispatch(
            notification_type=TYPE_DAILY,
            source_id=source_id,
            template_name=template,
            body_params=[
                str(email_count_i),
                str(important_i),
                str(pending_i),
            ],
            payload_summary={
                "email_count": email_count_i,
                "important_updates": important_i,
                "pending_actions": pending_i,
                "meetings": meetings_i,
                "briefing_date": payload.get("briefing_date"),
            },
        )
    except Exception:
        logger.exception("notify_whatsapp_daily_briefing crashed")
        return {"ok": False, "status": "failed"}


def notify_whatsapp_important_email(email_row: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """After high-importance analysis save. Never raises."""
    try:
        importance = str((analysis or {}).get("importance") or "").lower()
        if importance != "high":
            return {"ok": True, "status": "skipped"}

        gmail_message_id = (
            (email_row or {}).get("gmail_message_id")
            or (email_row or {}).get("id")
        )
        if not gmail_message_id:
            return {"ok": False, "status": "skipped", "error_message": "missing source id"}

        template = _template_name(
            "WHATSAPP_TEMPLATE_IMPORTANT_EMAIL", "buddy_important_email"
        )
        sender = truncate_param((email_row or {}).get("sender"), 120)
        subject = truncate_param((email_row or {}).get("subject"), 120)
        summary = truncate_param((analysis or {}).get("summary"), 200)
        return _dispatch(
            notification_type=TYPE_IMPORTANT,
            source_id=str(gmail_message_id),
            template_name=template,
            body_params=[sender, subject, summary],
            payload_summary={
                "sender": sender,
                "subject": subject,
                "summary": summary,
                "email_id": (email_row or {}).get("id"),
            },
        )
    except Exception:
        logger.exception("notify_whatsapp_important_email crashed")
        return {"ok": False, "status": "failed"}


def notify_whatsapp_meeting_and_payment_from_briefing(
    payload: dict[str, Any] | None,
    *,
    briefing_day: date | None = None,
) -> dict[str, Any]:
    """Scaffold: enqueue same-day confirmed meetings / due deadlines if templates set."""
    summary = {
        "meetings_enqueued": 0,
        "payments_enqueued": 0,
        "results": [],
    }
    try:
        if not whatsapp_enabled():
            summary["status"] = "disabled"
            return summary

        payload = payload or {}
        day = briefing_day
        if day is None:
            raw = str(payload.get("briefing_date") or "")[:10]
            try:
                day = date.fromisoformat(raw) if raw else None
            except ValueError:
                day = None
        if day is None:
            summary["status"] = "skipped"
            return summary

        meeting_template = _template_name("WHATSAPP_TEMPLATE_MEETING", "")
        payment_template = _template_name("WHATSAPP_TEMPLATE_PAYMENT", "")

        for meeting in payload.get("meetings") or []:
            if not isinstance(meeting, dict):
                continue
            status = str(meeting.get("status") or "").lower()
            if status in {"cancelled", "tentative"}:
                continue
            if status and status not in {"confirmed", "rescheduled", "accepted"}:
                continue
            event_date = str(meeting.get("event_date") or "")[:10]
            if event_date != day.isoformat():
                continue
            source_email_id = meeting.get("source_email_id") or "unknown"
            source_id = f"meeting:{source_email_id}:{event_date}"
            if not meeting_template:
                continue
            title = truncate_param(meeting.get("event_title") or "Meeting", 120)
            start = truncate_param(meeting.get("start_time") or "TBD", 40)
            result = _dispatch(
                notification_type=TYPE_MEETING,
                source_id=source_id,
                template_name=meeting_template,
                body_params=[title, event_date, start],
                payload_summary={
                    "event_title": title,
                    "event_date": event_date,
                    "start_time": start,
                    "status": status or "confirmed",
                },
            )
            summary["results"].append(result)
            if result.get("status") not in {"skipped", "disabled", "pending_approval"}:
                summary["meetings_enqueued"] += 1

        for item in payload.get("upcoming_deadlines") or []:
            if not isinstance(item, dict):
                continue
            due = str(item.get("due_date") or "")[:10]
            if due != day.isoformat():
                continue
            title = str(item.get("title") or item.get("description") or "")
            if not _payment_hint(title) and not _payment_hint(
                str(item.get("recommended_action") or "")
            ):
                continue
            if not payment_template:
                continue
            source_email_id = item.get("source_email_id") or "unknown"
            source_id = f"deadline:{source_email_id}:{due}"
            result = _dispatch(
                notification_type=TYPE_PAYMENT,
                source_id=source_id,
                template_name=payment_template,
                body_params=[
                    truncate_param(title, 120),
                    due,
                ],
                payload_summary={"title": truncate_param(title, 120), "due_date": due},
            )
            summary["results"].append(result)
            if result.get("status") not in {"skipped", "disabled", "pending_approval"}:
                summary["payments_enqueued"] += 1

        summary["status"] = "ok"
        return summary
    except Exception:
        logger.exception("notify_whatsapp_meeting_and_payment_from_briefing crashed")
        summary["status"] = "failed"
        return summary


def _payment_hint(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(payment|pay by|invoice|renewal|filing fee|amount due|due amount)\b",
            text or "",
            re.I,
        )
    )


def flush_whatsapp_outbox(*, limit: int = 20) -> dict[str, int]:
    """Retry queued/failed outbox rows with bounded retries. Never raises."""
    stats = {"attempted": 0, "accepted": 0, "failed": 0, "skipped": 0}
    try:
        if not whatsapp_enabled():
            return stats
        from db import list_whatsapp_outbox_for_flush, update_whatsapp_outbox_status

        rows = list_whatsapp_outbox_for_flush(limit=limit)
        for row in rows:
            stats["attempted"] += 1
            template = row.get("template_name") or ""
            params: list[str] = []
            summary = row.get("payload_summary") or {}
            ntype = row.get("notification_type")
            if ntype == TYPE_DAILY:
                params = [
                    str(summary.get("email_count", "0")),
                    str(summary.get("important_updates", "0")),
                    str(summary.get("pending_actions", "0")),
                ]
            elif ntype == TYPE_IMPORTANT:
                params = [
                    str(summary.get("sender") or "-"),
                    str(summary.get("subject") or "-"),
                    str(summary.get("summary") or "-"),
                ]
            elif ntype == TYPE_MEETING:
                params = [
                    str(summary.get("event_title") or "Meeting"),
                    str(summary.get("event_date") or "-"),
                    str(summary.get("start_time") or "TBD"),
                ]
            elif ntype == TYPE_PAYMENT:
                params = [
                    str(summary.get("title") or "Payment"),
                    str(summary.get("due_date") or "-"),
                ]

            send_result = send_template(
                to=normalize_whatsapp_number(row.get("recipient")),
                template_name=template,
                language_code=row.get("template_language") or _language(),
                body_params=params,
            )
            status = send_result.get("status") or "failed"
            try:
                update_whatsapp_outbox_status(
                    str(row.get("id")),
                    status=status if status != "accepted" else "accepted",
                    meta_message_id=send_result.get("meta_message_id"),
                    error_code=send_result.get("error_code"),
                    error_message=send_result.get("error_message"),
                    increment_retry=not send_result.get("ok"),
                )
            except Exception:
                logger.exception("Outbox flush status update failed id=%s", row.get("id"))
            if send_result.get("ok"):
                stats["accepted"] += 1
            else:
                stats["failed"] += 1
        return stats
    except Exception:
        logger.exception("flush_whatsapp_outbox crashed")
        return stats
