"""Unit tests for structured briefing hydration (no OpenAI / DB)."""

from datetime import date

from briefing_service import generate_briefing_content
from briefing_structure import (
    BriefingContent,
    build_overview,
    extract_analysis_deadlines,
    hydrate_briefing_payload,
    parse_calendar_invite,
    parse_due_date,
)


HR_INVITE = {
    "id": "email-hr",
    "subject": (
        "Invitation: Gaurang / HR @ Tue Nov 3, 2026 2:30am - 3:30am (IST) "
        "(Gaurang Bhargava)"
    ),
    "sender": "HR <hr@rbhargavaassociates.com>",
    "received_at": "2026-11-02T12:47:00+05:30",
    "snippet": "Join with Google Meet – You have been invited by HR",
    "email_analyses": [],
}


def test_empty_inbox_briefing_without_llm():
    content = generate_briefing_content([])
    assert isinstance(content, BriefingContent)
    assert content.headline == "Quiet inbox"
    assert content.items == []
    assert content.meetings == []


def test_calendar_invite_is_tentative_and_uses_event_date():
    parsed = parse_calendar_invite(HR_INVITE)
    assert parsed is not None
    assert parsed["event_title"] == "Gaurang / HR"
    assert parsed["event_date"] == "2026-11-03"
    assert parsed["start_time"] == "2:30 AM"
    assert parsed["end_time"] == "3:30 AM"
    assert parsed["status"] == "tentative"
    assert parsed["source_email_id"] == "email-hr"
    assert parsed["location"] == "Google Meet"


def test_cancelled_invite_status():
    email = {
        **HR_INVITE,
        "subject": "Canceled event: Gaurang / HR @ Tue Nov 3, 2026 2:30am - 3:30am (IST)",
    }
    parsed = parse_calendar_invite(email)
    assert parsed is not None
    assert parsed["status"] == "cancelled"


def test_meetings_only_on_event_date_not_received_date():
    content = BriefingContent(
        headline="Day",
        meetings=[
            {
                "event_title": "Gaurang / HR",
                "event_date": "2026-11-03",
                "start_time": "2:30 AM",
                "source_email_index": 1,
                "status": "tentative",
            }
        ],
    )
    nov2 = hydrate_briefing_payload(content, [HR_INVITE], briefing_day=date(2026, 11, 2))
    nov3 = hydrate_briefing_payload(content, [HR_INVITE], briefing_day=date(2026, 11, 3))
    assert nov2["meetings"] == []
    assert len(nov3["meetings"]) == 1
    assert nov3["meetings"][0]["source_email_id"] == "email-hr"


def test_duplicate_event_emails_collapse():
    invite = parse_calendar_invite(HR_INVITE)
    accepted = parse_calendar_invite(
        {
            **HR_INVITE,
            "id": "email-hr-2",
            "subject": "Accepted: Gaurang / HR @ Tue Nov 3, 2026 2:30am - 3:30am (IST)",
        }
    )
    payload = hydrate_briefing_payload(
        BriefingContent(headline="Day"),
        [HR_INVITE],
        briefing_day=date(2026, 11, 3),
        extra_meetings=[invite, accepted],
    )
    assert len(payload["meetings"]) == 1
    assert payload["meetings"][0]["status"] == "confirmed"


def test_hydrate_attaches_email_metadata_from_index():
    emails = [
        {
            "id": "uuid-1",
            "subject": "GST Compliance Update",
            "sender": "Taxmann <alert@newsletter.taxmann.com>",
            "received_at": "2026-11-02T18:08:00+05:30",
            "email_analyses": [{"importance": "high", "action_required": True, "deadlines": []}],
        }
    ]
    content = BriefingContent(
        headline="Tax day",
        items=[
            {
                "title": "GST Filing Deadline",
                "description": "A GST compliance update mentions an upcoming filing deadline.",
                "priority": "high",
                "recommended_action": "Review the GST filing requirements.",
                "source_email_index": 1,
                "kind": "action",
            }
        ],
    )
    payload = hydrate_briefing_payload(content, emails, briefing_day=date(2026, 11, 2))
    item = payload["items"][0]
    assert item["source_email_id"] == "uuid-1"
    assert item["sender"] == "Taxmann"
    assert item["subject"] == "GST Compliance Update"
    assert "source_email_index" not in item


def test_wrong_index_is_rematched_by_title():
    emails = [
        {
            "id": "icai-1",
            "subject": "1st Online Batch of Certificate Course on UK Taxation",
            "sender": "ukct@icai.in",
            "received_at": "2026-11-03T10:00:00+05:30",
            "snippet": "Submission of details for dispatch of study material",
            "email_analyses": [{"importance": "high", "action_required": True, "deadlines": []}],
        },
        {
            "id": "nippon-1",
            "subject": "Start Your SIP in our Nippon India Growth Mid Cap Fund",
            "sender": "Nippon Life",
            "received_at": "2026-11-03T10:10:00+05:30",
            "snippet": "Start SIP today",
            "email_analyses": [{"importance": "medium", "action_required": False, "deadlines": []}],
        },
    ]
    content = BriefingContent(
        headline="Day",
        items=[
            {
                "title": "Start SIP in Nippon India Growth Fund",
                "description": "Marketing note about a SIP fund.",
                "priority": "medium",
                "source_email_index": 1,
                "kind": "update",
            }
        ],
    )
    payload = hydrate_briefing_payload(content, emails, briefing_day=date(2026, 11, 3))
    assert payload["items"][0]["source_email_id"] == "nippon-1"


def test_duplicate_meetings_same_day_collapse_despite_time_format():
    payload = hydrate_briefing_payload(
        BriefingContent(headline="Day"),
        [HR_INVITE],
        briefing_day=date(2026, 11, 3),
        extra_meetings=[
            {
                "event_title": "Gaurang / HR",
                "event_date": "2026-11-03",
                "start_time": "2:30 PM",
                "status": "rescheduled",
                "source_email_id": "email-hr",
            },
            {
                "event_title": "Gaurang / HR",
                "event_date": "2026-11-03",
                "start_time": "14:30",
                "status": "confirmed",
                "source_email_id": "email-hr",
            },
        ],
    )
    assert len(payload["meetings"]) == 1
    assert payload["meetings"][0]["status"] == "rescheduled"
    assert payload["meetings"][0]["start_time"] in {"14:30", "2:30 PM"}


def test_overview_counts_come_from_structured_results():
    overview = build_overview(
        [
            {"priority": "high", "kind": "update", "recommended_action": ""},
            {"priority": "high", "kind": "action", "recommended_action": "Reply"},
            {"priority": "medium", "kind": "action", "recommended_action": "Review"},
        ],
        [
            {"status": "tentative"},
            {"status": "cancelled"},
        ],
    )
    assert overview["important_updates"] == 2
    assert overview["pending_actions"] == 2
    assert overview["meetings"] == 1
    assert overview["text"].startswith("Today you have")
    assert "2 important updates" in overview["text"]
    assert "1 meeting" in overview["text"]


def test_missing_meeting_time_stays_empty():
    payload = hydrate_briefing_payload(
        BriefingContent(
            headline="Day",
            meetings=[
                {
                    "event_title": "Webinar",
                    "event_date": "2026-11-02",
                    "start_time": "",
                    "status": "tentative",
                    "source_email_index": 1,
                }
            ],
        ),
        [{"id": "e1", "subject": "Webinar", "sender": "Zoom", "received_at": "2026-11-01"}],
        briefing_day=date(2026, 11, 2),
    )
    assert payload["meetings"][0]["start_time"] == ""


def test_past_deadlines_excluded_unless_overdue():
    email = {
        "id": "d1",
        "subject": "Filing",
        "sender": "ICAI <icai@example.com>",
        "received_at": "2026-11-01",
        "email_analyses": {
            "importance": "high",
            "action_required": True,
            "deadlines": ["October 1, 2026", "December 31, 2026"],
        },
    }
    found = extract_analysis_deadlines(email, date(2026, 11, 2))
    dates = {item["due_date"] for item in found}
    assert "2026-10-01" not in dates
    assert "2026-12-31" in dates


def test_newsletter_dates_are_not_personal_deadlines():
    email = {
        "id": "n1",
        "subject": "Taxmann Daily",
        "sender": "Taxmann <alert@newsletter.taxmann.com>",
        "email_analyses": {
            "importance": "medium",
            "action_required": False,
            "deadlines": ["December 31, 2026"],
        },
    }
    assert extract_analysis_deadlines(email, date(2026, 11, 2)) == []


def test_promo_webinar_dates_are_not_personal_deadlines():
    email = {
        "id": "p1",
        "subject": "Unlock AI business value with Microsoft Discovery Sessions",
        "sender": "Microsoft <noreply@microsoft.com>",
        "email_analyses": {
            "importance": "high",
            "action_required": True,
            "deadlines": ["November 9, 2026", "November 10, 2026"],
        },
    }
    assert extract_analysis_deadlines(email, date(2026, 11, 3)) == []


def test_legacy_briefing_renders_without_source_ids():
    class Legacy:
        headline = "Old"
        highlights = ["Read the GST note"]
        deadlines = ["December 31, 2026"]
        action_items = ["Reply to HR"]
        notes = ""

    payload = hydrate_briefing_payload(Legacy(), [], briefing_day=date(2026, 11, 2))
    assert payload["items"]
    assert payload["items"][0]["source_email_id"] is None
    assert payload["upcoming_deadlines"][0]["due_date"] == "2026-12-31"
    assert payload["overview"]["text"]


def test_parse_due_date_formats():
    assert parse_due_date("December 31, 2026") == date(2026, 12, 31)
    assert parse_due_date("due 2026-11-15") == date(2026, 11, 15)
    assert parse_due_date("within one month from the end of the month") is None


if __name__ == "__main__":
    test_empty_inbox_briefing_without_llm()
    test_calendar_invite_is_tentative_and_uses_event_date()
    test_cancelled_invite_status()
    test_meetings_only_on_event_date_not_received_date()
    test_duplicate_event_emails_collapse()
    test_hydrate_attaches_email_metadata_from_index()
    test_wrong_index_is_rematched_by_title()
    test_duplicate_meetings_same_day_collapse_despite_time_format()
    test_overview_counts_come_from_structured_results()
    test_missing_meeting_time_stays_empty()
    test_past_deadlines_excluded_unless_overdue()
    test_newsletter_dates_are_not_personal_deadlines()
    test_promo_webinar_dates_are_not_personal_deadlines()
    test_legacy_briefing_renders_without_source_ids()
    test_parse_due_date_formats()
    print("All briefing_structure tests passed.")
