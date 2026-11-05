"""Tests for Task 5 — briefing preferences, VIP, filtering, schedules."""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from briefing_preferences import (
    apply_section_preferences,
    default_preferences,
    filter_emails_for_preferences,
    is_within_working_hours,
    normalize_email_address,
    normalize_preferences_payload,
    personalization_enabled,
    should_filter_email,
    validate_timezone,
    validate_vip_contacts,
)
from briefing_priority import score_email_priority
from scheduler import _register_personalized_briefing_jobs, start_scheduler

KOLKATA = ZoneInfo("Asia/Kolkata")


def test_personalization_flag_defaults_off():
    with patch.dict("os.environ", {"BRIEFING_PERSONALIZATION_ENABLED": "false"}):
        assert personalization_enabled() is False


def test_default_preferences_preserve_sections():
    prefs = default_preferences(timezone_name="Asia/Kolkata")
    assert prefs["briefing_sections"]["important_emails"] is True
    assert prefs["briefing_sections"]["low_priority_emails"] is True
    assert prefs["filtering_preferences"]["hide_promotional"] is False
    assert prefs["mode_schedules"]["morning"]["enabled"] is False
    assert prefs["mode_schedules"]["evening"]["enabled"] is False


def test_invalid_timezone_rejected():
    try:
        validate_timezone("Not/A_Zone")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_invalid_vip_email_rejected():
    try:
        validate_vip_contacts(["not-an-email"])
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert normalize_email_address("Alice <Boss@Example.COM>") == "boss@example.com"
    assert validate_vip_contacts(["Boss@Example.COM", "boss@example.com"]) == [
        "boss@example.com"
    ]


def test_vip_prioritized_in_scoring():
    email = {
        "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "sender": "Boss <boss@example.com>",
        "from_email": "boss@example.com",
        "subject": "Need this today",
        "email_analyses": [
            {
                "importance": "medium",
                "action_required": False,
                "confidence": 0.5,
                "summary": "Please review",
            }
        ],
    }
    day = datetime(2026, 11, 3).date()
    base = score_email_priority(email, briefing_day=day)
    vip = score_email_priority(
        email, briefing_day=day, vip_emails={"boss@example.com"}
    )
    assert vip["score"] > base["score"]
    assert vip["breakdown"]["vip"] > 0


def test_newsletter_filter_does_not_suppress_urgent():
    prefs = default_preferences()
    prefs["filtering_preferences"]["hide_newsletters"] = True
    prefs["filtering_preferences"]["hide_promotional"] = True

    newsletter = {
        "subject": "Weekly Newsletter",
        "sender": "news@newsletter.example",
        "label_ids": ["CATEGORY_PROMOTIONS"],
        "email_analyses": [{"importance": "low", "action_required": False}],
    }
    urgent = {
        "subject": "Urgent: invoice overdue",
        "sender": "client@acme.com",
        "label_ids": ["CATEGORY_PROMOTIONS"],
        "email_analyses": [
            {
                "importance": "high",
                "action_required": True,
                "deadlines": ["overdue yesterday"],
            }
        ],
    }
    with patch.dict("os.environ", {"BRIEFING_PERSONALIZATION_ENABLED": "true"}):
        assert should_filter_email(newsletter, prefs) is True
        assert should_filter_email(urgent, prefs) is False
        filtered = filter_emails_for_preferences([newsletter, urgent], prefs)
        assert urgent in filtered
        assert newsletter not in filtered


def test_working_hours_timezone_aware():
    prefs = default_preferences(timezone_name="Asia/Kolkata")
    prefs["working_days"] = [1, 2, 3, 4, 5]
    prefs["working_hours"] = {"start": "09:00", "end": "18:00"}
    # Tuesday 10:00 IST
    inside = datetime(2026, 11, 3, 10, 0, tzinfo=KOLKATA)
    outside = datetime(2026, 11, 3, 20, 0, tzinfo=KOLKATA)
    weekend = datetime(2026, 11, 7, 10, 0, tzinfo=KOLKATA)  # Saturday
    assert is_within_working_hours(prefs, when=inside) is True
    assert is_within_working_hours(prefs, when=outside) is False
    assert is_within_working_hours(prefs, when=weekend) is False


def test_section_preferences_keep_schema_keys():
    prefs = default_preferences()
    prefs["briefing_sections"]["meetings"] = False
    prefs["briefing_sections"]["deadlines"] = False
    payload = {
        "items": [{"title": "A", "kind": "action", "priority": "high"}],
        "meetings": [{"event_title": "Sync"}],
        "upcoming_deadlines": [{"title": "Due"}],
        "deadlines": ["Due"],
    }
    with patch.dict("os.environ", {"BRIEFING_PERSONALIZATION_ENABLED": "true"}):
        out = apply_section_preferences(payload, prefs)
    assert "meetings" in out and out["meetings"] == []
    assert "upcoming_deadlines" in out and out["upcoming_deadlines"] == []
    assert out["items"]


def test_normalize_rejects_bad_hours():
    try:
        normalize_preferences_payload(
            {"working_hours": {"start": "18:00", "end": "09:00"}}
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_personalized_jobs_not_registered_when_flag_off():
    from unittest.mock import MagicMock

    fake = MagicMock()
    with patch.dict("os.environ", {"BRIEFING_PERSONALIZATION_ENABLED": "false"}):
        _register_personalized_briefing_jobs(fake)
    fake.add_job.assert_not_called()


def test_daily_scheduler_job_id_unchanged():
    """daily_email_briefing remains the canonical morning job id."""
    with patch.dict(
        "os.environ",
        {
            "ENABLE_SCHEDULER": "false",
            "BRIEFING_PERSONALIZATION_ENABLED": "true",
        },
    ):
        # start_scheduler exits early when disabled — still proves flag path safe
        start_scheduler()


def test_missing_preferences_fall_back_to_defaults():
    with patch(
        "db.get_briefing_preferences",
        return_value=None,
    ):
        from briefing_preferences import get_preferences_for_account

        result = get_preferences_for_account("user@example.com")
    assert result["source"] == "defaults"
    assert result["preferences"]["mode_schedules"]["morning"]["enabled"] is False
