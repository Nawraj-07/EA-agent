"""Structured daily briefing models, hydration, and meeting/deadline parsing.

Stored inside daily_briefings.content JSONB — no schema migration.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

Priority = Literal["high", "medium", "low"]
MeetingStatus = Literal["confirmed", "tentative", "cancelled", "rescheduled"]

MAX_ITEMS = 6
MAX_MEETINGS = 8
MAX_DEADLINES = 6

EVENTISH_RE = re.compile(
    r"invitation:|updated invitation|canceled event|cancelled event|"
    r"webinar|interview|appointment|zoom\.us|meet\.google|"
    r"teams\.microsoft|join with google meet|calendar|"
    r"deadline|due date|rsvp|conference call|meeting invite",
    re.I,
)

INVITE_SUBJECT_RE = re.compile(
    r"^(?P<prefix>Updated invitation|Canceled event|Cancelled event|"
    r"Invitation|Accepted):\s*(?P<title>.+?)\s*@\s*(?P<when>.+)$",
    re.I,
)

WHEN_RE = re.compile(
    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\s+(?P<day>\d{1,2}),\s+(?P<year>\d{4})"
    r"(?:\s+(?P<start>\d{1,2}:\d{2}\s*[ap]m)"
    r"(?:\s*[-–]\s*(?P<end>\d{1,2}:\d{2}\s*[ap]m))?)?",
    re.I,
)

MEET_LINK_RE = re.compile(
    r"(https?://(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com)[^\s<>]+)",
    re.I,
)

SENDER_RE = re.compile(r"^(.*?)\s*<[^>]+>\s*$")

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

STATUS_RANK = {
    "tentative": 1,
    "confirmed": 2,
    "rescheduled": 3,
    "cancelled": 4,
}

DUE_DATE_FORMATS = (
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
)


class BriefingItem(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    priority: Priority = "medium"
    recommended_action: str = ""
    source_email_index: int | None = None
    kind: Literal["update", "action"] = "update"


class MeetingItem(BaseModel):
    event_title: str = Field(..., min_length=1)
    event_date: str = ""
    start_time: str = ""
    end_time: str = ""
    location: str = ""
    status: MeetingStatus = "tentative"
    source_email_index: int | None = None
    organizer: str = ""


class DeadlineItem(BaseModel):
    title: str = Field(..., min_length=1)
    due_date: str = ""
    priority: Priority = "medium"
    recommended_action: str = ""
    source_email_index: int | None = None
    overdue: bool = False
    is_personal_obligation: bool = True


class BriefingOverview(BaseModel):
    important_updates: int = 0
    pending_actions: int = 0
    meetings: int = 0
    text: str = ""


class BriefingContent(BaseModel):
    headline: str = Field(..., min_length=1)
    highlights: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    notes: str = Field(
        default="",
        description="Short note when inbox is quiet or context is limited",
    )
    items: list[BriefingItem] = Field(default_factory=list)
    meetings: list[MeetingItem] = Field(default_factory=list)
    upcoming_deadlines: list[DeadlineItem] = Field(default_factory=list)


def sender_display(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    match = SENDER_RE.match(value)
    name = (match.group(1) if match else value).strip().strip('"')
    return name or value


def looks_eventish(email: dict) -> bool:
    text = " ".join(
        str(email.get(key) or "")
        for key in ("subject", "sender", "snippet")
    )
    return bool(EVENTISH_RE.search(text))


def _analysis(email: dict) -> dict:
    analyses = email.get("email_analyses") or []
    if isinstance(analyses, list):
        return analyses[0] if analyses else {}
    return analyses if isinstance(analyses, dict) else {}


def parse_due_date(text: str | None) -> date | None:
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw)
    if iso:
        try:
            return date.fromisoformat(iso.group(1))
        except ValueError:
            pass
    named = re.search(
        r"\b(January|February|March|April|May|June|July|August|"
        r"September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\.?\s+(\d{1,2}),?\s+(\d{4})\b",
        raw,
        re.I,
    )
    if named:
        try:
            month = MONTHS[named.group(1)[:3].lower()]
            return date(int(named.group(3)), month, int(named.group(2)))
        except ValueError:
            pass
    for fmt in DUE_DATE_FORMATS:
        try:
            return datetime.strptime(raw[:32], fmt).date()
        except ValueError:
            continue
    return None


def _parse_clock(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", raw.strip().upper().replace(".", ""))
    match = re.match(r"^(\d{1,2}):(\d{2})\s*([AP]M)$", text)
    if not match:
        return text.title()
    hour = int(match.group(1))
    minute = match.group(2)
    suffix = match.group(3)
    return f"{hour}:{minute} {suffix}"


def _parse_when_chunk(when: str) -> tuple[str, str, str]:
    match = WHEN_RE.search(when or "")
    if not match:
        return "", "", ""
    try:
        event_date = date(
            int(match.group("year")),
            MONTHS[match.group("mon")[:3].lower()],
            int(match.group("day")),
        ).isoformat()
    except (KeyError, ValueError):
        event_date = ""
    return (
        event_date,
        _parse_clock(match.group("start")),
        _parse_clock(match.group("end")),
    )


def _invite_status(prefix: str) -> MeetingStatus:
    key = prefix.lower()
    if "cancel" in key:
        return "cancelled"
    if "updated" in key:
        return "rescheduled"
    if "accepted" in key:
        return "confirmed"
    return "tentative"


def _meeting_link(email: dict) -> str:
    blob = " ".join(
        str(email.get(key) or "")
        for key in ("snippet", "body", "subject")
    )
    match = MEET_LINK_RE.search(blob)
    if match:
        return match.group(1).rstrip(").,;")
    if "join with google meet" in blob.lower() or "meet.google.com" in blob.lower():
        return "Google Meet"
    if "zoom.us" in blob.lower():
        return "Zoom"
    return ""


def parse_calendar_invite(email: dict) -> dict | None:
    subject = str(email.get("subject") or "").strip()
    match = INVITE_SUBJECT_RE.match(subject)
    if not match:
        return None
    title = re.sub(r"\s+\([^)]*\)\s*$", "", match.group("title")).strip()
    event_date, start_time, end_time = _parse_when_chunk(match.group("when"))
    if not title:
        return None
    return {
        "event_title": title[:160],
        "event_date": event_date,
        "start_time": start_time,
        "end_time": end_time,
        "location": _meeting_link(email),
        "status": _invite_status(match.group("prefix")),
        "organizer": sender_display(email.get("sender")),
        "source_email_id": email.get("id") or "",
        "sender": sender_display(email.get("sender")),
        "subject": email.get("subject") or "",
        "received_at": email.get("received_at") or "",
    }


PROMO_DEADLINE_RE = re.compile(
    r"newsletter|marketing@|noreply@email\.openai|linkedin\.com|"
    r"discovery session|holiday playbook|start your sip|mutual fund|"
    r"economictimes|taxmann|taxtmi|webinar|ads platform",
    re.I,
)
PERSONAL_DEADLINE_RE = re.compile(
    r"\b(renew|renewal|payment due|pay by|filing|submit|due date|last date|"
    r"ebill|e-bill|invoice|policy)\b",
    re.I,
)


def extract_analysis_deadlines(email: dict, briefing_day: date) -> list[dict]:
    analysis = _analysis(email)
    out: list[dict] = []
    sender = str(email.get("sender") or "")
    subject = str(email.get("subject") or "").strip()
    for raw in analysis.get("deadlines") or []:
        due = parse_due_date(str(raw))
        if due is None:
            continue
        overdue_marked = "overdue" in str(raw).lower()
        overdue = due < briefing_day
        if overdue and not overdue_marked:
            continue
        evidence = f"{sender}\n{subject}\n{raw}"
        if PROMO_DEADLINE_RE.search(evidence) and not PERSONAL_DEADLINE_RE.search(
            evidence
        ):
            continue
        is_newsletter = "taxmann" in sender.lower() or "newsletter" in sender.lower()
        if is_newsletter and not analysis.get("action_required"):
            continue
        raw_title = str(raw).strip()
        # Prefer a readable subject-based title when the stored deadline is only a date.
        if parse_due_date(raw_title) and subject:
            title = subject[:120]
        else:
            title = raw_title[:160]
        out.append(
            {
                "title": title,
                "due_date": due.isoformat(),
                "priority": (analysis.get("importance") or "medium").lower()
                if (analysis.get("importance") or "").lower() in {"low", "medium", "high"}
                else "medium",
                "recommended_action": "Review the deadline and complete any required filing or reply."
                if analysis.get("action_required")
                else "",
                "source_email_id": email.get("id") or "",
                "sender": sender_display(email.get("sender")),
                "subject": subject,
                "received_at": email.get("received_at") or "",
                "overdue": overdue_marked,
                "is_personal_obligation": True,
            }
        )
    return out


def _email_by_index(emails: list[dict]) -> dict[int, dict]:
    return {index: email for index, email in enumerate(emails, start=1)}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{4,}", (text or "").lower())
        if token
        not in {
            "with",
            "from",
            "this",
            "that",
            "your",
            "have",
            "meeting",
            "email",
            "update",
            "please",
            "about",
        }
    }


def _title_matches_email(title: str, email: dict) -> bool:
    title_tokens = _tokens(title)
    if not title_tokens:
        return False
    hay = " ".join(
        str(email.get(key) or "")
        for key in ("subject", "snippet", "sender")
    ).lower()
    hits = sum(1 for token in title_tokens if token in hay)
    return hits >= max(1, min(2, len(title_tokens) // 2))


def _find_email_for_row(row: dict, emails: list[dict], index_key: str = "source_email_index"):
    indexed = _email_by_index(emails)
    idx = row.get(index_key)
    title = str(row.get("title") or row.get("event_title") or "")
    email = None
    if isinstance(idx, int) and idx in indexed:
        candidate = indexed[idx]
        if not title or _title_matches_email(title, candidate):
            email = candidate
    source_id = str(row.get("source_email_id") or "").strip()
    if email is None and source_id:
        candidate = next(
            (item for item in emails if str(item.get("id") or "") == source_id),
            None,
        )
        if candidate and (not title or _title_matches_email(title, candidate)):
            email = candidate
    if email is None and title:
        for candidate in emails:
            if _title_matches_email(title, candidate):
                email = candidate
                break
    return email


def _hydrate_source(row: dict, emails: list[dict], index_key: str = "source_email_index") -> dict:
    email = _find_email_for_row(row, emails, index_key=index_key)
    if email is None:
        row["source_email_id"] = str(row.get("source_email_id") or "").strip() or None
        row.setdefault("sender", "")
        row.setdefault("subject", "")
        row.setdefault("received_at", "")
        row.pop(index_key, None)
        return row
    row["source_email_id"] = email.get("id")
    row["sender"] = sender_display(email.get("sender"))
    row["subject"] = email.get("subject") or ""
    row["received_at"] = email.get("received_at") or ""
    if not row.get("organizer"):
        row["organizer"] = row["sender"]
    if not row.get("location"):
        row["location"] = _meeting_link(email)
    row.pop(index_key, None)
    return row


def _normalize_clock(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    ampm = re.match(r"^(\d{1,2}):(\d{2})\s*([AaPp][Mm])$", text)
    if ampm:
        hour = int(ampm.group(1)) % 12
        if ampm.group(3).lower() == "pm":
            hour += 12
        return f"{hour:02d}:{ampm.group(2)}"
    hm = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if hm:
        return f"{int(hm.group(1)):02d}:{hm.group(2)}"
    return text


def _meeting_dedupe_key(meeting: dict) -> str:
    title = re.sub(r"\s+", " ", str(meeting.get("event_title") or "").strip().lower())
    title = re.sub(r"\b(meeting|webinar|event|invite|invitation)\b", "", title)
    title = re.sub(r"[^a-z0-9/]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return f"{title}|{meeting.get('event_date') or ''}"


def _dedupe_meetings(meetings: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for meeting in meetings:
        key = _meeting_dedupe_key(meeting)
        meeting = dict(meeting)
        meeting["start_time"] = _normalize_clock(meeting.get("start_time"))
        meeting["end_time"] = _normalize_clock(meeting.get("end_time"))
        existing = best.get(key)
        if existing is None:
            best[key] = meeting
            continue
        existing_rank = STATUS_RANK.get(existing.get("status") or "", 0)
        new_rank = STATUS_RANK.get(meeting.get("status") or "", 0)
        if new_rank > existing_rank:
            if not meeting.get("start_time"):
                meeting["start_time"] = existing.get("start_time") or ""
            if not meeting.get("end_time"):
                meeting["end_time"] = existing.get("end_time") or ""
            best[key] = meeting
            continue
        if new_rank == existing_rank and meeting.get("start_time") and not existing.get(
            "start_time"
        ):
            best[key] = meeting
    return list(best.values())


def _deterministic_items(emails: list[dict]) -> list[dict]:
    items: list[dict] = []
    for email in emails:
        analysis = _analysis(email)
        importance = _normalize_priority(analysis.get("importance"))
        action_required = bool(analysis.get("action_required"))
        if importance not in {"high", "medium"} and not action_required:
            continue
        if importance == "medium" and not action_required:
            continue
        summary = str(analysis.get("summary") or email.get("snippet") or "").strip()
        subject = str(email.get("subject") or "Email update").strip()
        title = subject[:120]
        items.append(
            {
                "title": title,
                "description": summary[:420]
                or "Review this email for details relevant to today's briefing.",
                "priority": importance if importance in {"high", "medium", "low"} else "medium",
                "recommended_action": "Review this email and take any required action."
                if action_required
                else "",
                "kind": "action" if action_required else "update",
                "source_email_id": email.get("id"),
                "sender": sender_display(email.get("sender")),
                "subject": subject,
                "received_at": email.get("received_at") or "",
            }
        )
        if len(items) >= MAX_ITEMS:
            break
    return items


def _normalize_priority(value: str | None) -> str:
    raw = (value or "medium").strip().lower()
    return raw if raw in {"low", "medium", "high"} else "medium"


def _normalize_status(value: str | None) -> str:
    raw = (value or "tentative").strip().lower()
    return raw if raw in STATUS_RANK else "tentative"


def build_overview(
    items: list[dict],
    meetings: list[dict],
    *,
    briefing_day: date | None = None,
) -> dict:
    important = sum(1 for item in items if item.get("priority") == "high")
    actions = sum(
        1
        for item in items
        if item.get("kind") == "action" or str(item.get("recommended_action") or "").strip()
    )
    meeting_n = sum(1 for meeting in meetings if meeting.get("status") != "cancelled")

    def phrase(count: int, singular: str, plural: str) -> str:
        return f"{count} {singular if count == 1 else plural}"

    text = (
        f"Today you have {phrase(important, 'important update', 'important updates')}, "
        f"{phrase(actions, 'pending action', 'pending actions')}, and "
        f"{phrase(meeting_n, 'meeting', 'meetings')} detected in your emails."
    )
    return {
        "important_updates": important,
        "pending_actions": actions,
        "meetings": meeting_n,
        "text": text,
    }


def content_as_dict(content) -> dict:
    if hasattr(content, "model_dump"):
        return content.model_dump()
    return {
        "headline": getattr(content, "headline", None) or "Daily briefing",
        "highlights": list(getattr(content, "highlights", []) or []),
        "deadlines": list(getattr(content, "deadlines", []) or []),
        "action_items": list(getattr(content, "action_items", []) or []),
        "notes": getattr(content, "notes", "") or "",
        "items": list(getattr(content, "items", []) or []),
        "meetings": list(getattr(content, "meetings", []) or []),
        "upcoming_deadlines": list(getattr(content, "upcoming_deadlines", []) or []),
    }


def _legacy_items(payload: dict) -> list[dict]:
    items: list[dict] = []
    for text in payload.get("action_items") or []:
        title = str(text).strip()
        if not title:
            continue
        items.append(
            {
                "title": title[:120],
                "description": title,
                "priority": "medium",
                "recommended_action": title,
                "kind": "action",
                "source_email_id": None,
                "sender": "",
                "subject": "",
                "received_at": "",
            }
        )
    for text in payload.get("highlights") or []:
        title = str(text).strip()
        if not title:
            continue
        items.append(
            {
                "title": title[:120],
                "description": title,
                "priority": "medium",
                "recommended_action": "",
                "kind": "update",
                "source_email_id": None,
                "sender": "",
                "subject": "",
                "received_at": "",
            }
        )
    return items[:MAX_ITEMS]


def _legacy_deadlines(payload: dict, briefing_day: date) -> list[dict]:
    out: list[dict] = []
    for text in payload.get("deadlines") or []:
        title = str(text).strip()
        if not title:
            continue
        due = parse_due_date(title)
        if due is not None and due < briefing_day:
            continue
        out.append(
            {
                "title": title[:160],
                "due_date": due.isoformat() if due else "",
                "priority": "medium",
                "recommended_action": "",
                "source_email_id": None,
                "sender": "",
                "subject": "",
                "received_at": "",
                "overdue": False,
            }
        )
    return out[:MAX_DEADLINES]


def hydrate_briefing_payload(
    content,
    emails: list[dict],
    *,
    briefing_day: date,
    extra_meetings: list[dict] | None = None,
    extra_deadlines: list[dict] | None = None,
) -> dict:
    payload = content_as_dict(content)
    items = [_hydrate_source(dict(item), emails) for item in payload.get("items") or []]
    for item in items:
        item["priority"] = _normalize_priority(item.get("priority"))
        item["kind"] = item.get("kind") if item.get("kind") in {"update", "action"} else "update"
        if not str(item.get("description") or "").strip():
            item["description"] = item.get("title") or ""
    # Prefer linked emails only; fall back to deterministic high-signal mail.
    linked_items = [item for item in items if item.get("source_email_id")]
    if linked_items:
        items = linked_items
    else:
        items = _deterministic_items(emails) or _legacy_items(payload)
    if len(items) < 3:
        seen_ids = {str(item.get("source_email_id") or "") for item in items}
        for candidate in _deterministic_items(emails):
            key = str(candidate.get("source_email_id") or "")
            if key and key in seen_ids:
                continue
            items.append(candidate)
            if key:
                seen_ids.add(key)
            if len(items) >= MAX_ITEMS:
                break
    items = items[:MAX_ITEMS]

    meetings = [_hydrate_source(dict(item), emails) for item in payload.get("meetings") or []]
    meetings.extend(extra_meetings or [])
    cleaned_meetings: list[dict] = []
    for meeting in meetings:
        meeting["status"] = _normalize_status(meeting.get("status"))
        event_date = str(meeting.get("event_date") or "").strip()[:10]
        parsed = parse_due_date(event_date) if event_date else None
        if parsed is None or parsed != briefing_day:
            continue
        meeting["event_date"] = parsed.isoformat()
        meeting["start_time"] = str(meeting.get("start_time") or "").strip()
        meeting["end_time"] = str(meeting.get("end_time") or "").strip()
        if not meeting.get("event_title"):
            continue
        if not meeting.get("source_email_id"):
            continue
        cleaned_meetings.append(meeting)
    meetings = _dedupe_meetings(cleaned_meetings)[:MAX_MEETINGS]

    # Prefer analysis-backed deadlines; keep LLM deadlines only with a linked email.
    deadlines = list(extra_deadlines or [])
    for item in payload.get("upcoming_deadlines") or []:
        hydrated = _hydrate_source(dict(item), emails)
        if hydrated.get("source_email_id"):
            deadlines.append(hydrated)
    cleaned_deadlines: list[dict] = []
    for item in deadlines:
        if item.get("is_personal_obligation") is False:
            continue
        due = parse_due_date(item.get("due_date") or item.get("title"))
        if due is None:
            continue
        overdue = due < briefing_day
        if overdue and not item.get("overdue"):
            continue
        item["due_date"] = due.isoformat()
        item["overdue"] = overdue
        item["priority"] = _normalize_priority(item.get("priority"))
        if not item.get("title") or not item.get("source_email_id"):
            continue
        cleaned_deadlines.append(item)
    if not cleaned_deadlines and not emails:
        cleaned_deadlines = _legacy_deadlines(payload, briefing_day)
    seen_deadline: set[str] = set()
    unique_deadlines: list[dict] = []
    for item in cleaned_deadlines:
        key = f"{item.get('due_date')}|{item.get('source_email_id') or (item.get('title') or '').strip().lower()}"
        if key in seen_deadline:
            continue
        seen_deadline.add(key)
        unique_deadlines.append(item)
    deadlines = unique_deadlines[:MAX_DEADLINES]

    overview = build_overview(items, meetings, briefing_day=briefing_day)

    payload["items"] = items
    payload["meetings"] = meetings
    payload["upcoming_deadlines"] = deadlines
    payload["overview"] = overview
    payload["highlights"] = payload.get("highlights") or [
        item.get("title") for item in items if item.get("kind") != "action"
    ]
    payload["action_items"] = payload.get("action_items") or [
        item.get("recommended_action") or item.get("title")
        for item in items
        if item.get("kind") == "action" or item.get("recommended_action")
    ]
    payload["deadlines"] = payload.get("deadlines") or [
        f"{item.get('title')} ({item.get('due_date')})" if item.get("due_date") else item.get("title")
        for item in deadlines
    ]
    return payload


def received_iso(email: dict, zone: ZoneInfo) -> str:
    raw = email.get("received_at")
    if isinstance(raw, str) and raw.strip():
        return raw
    internal = email.get("internal_date")
    if isinstance(internal, (int, float)) and internal > 0:
        dt = datetime.fromtimestamp(internal / 1000, tz=timezone.utc).astimezone(zone)
        return dt.isoformat()
    return ""
