"""Home Smart Briefing filters — scheduler mapping + Monday TZ visibility."""

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from scheduler import (
    _register_personalized_briefing_jobs,
    _register_smart_briefing_mode_jobs,
)


KOLKATA = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")


def _is_monday_in_tz(tz: ZoneInfo, when: datetime) -> bool:
    """Mirror frontend Monday visibility: weekday in configured IANA zone."""
    local = when.astimezone(tz)
    return local.weekday() == 0  # Monday


def test_monday_visible_only_on_monday_kolkata():
    monday = datetime(2026, 11, 2, 10, 0, tzinfo=KOLKATA)  # Mon
    tuesday = datetime(2026, 11, 3, 10, 0, tzinfo=KOLKATA)  # Tue
    sunday = datetime(2026, 11, 1, 23, 0, tzinfo=KOLKATA)  # Sun
    assert _is_monday_in_tz(KOLKATA, monday) is True
    assert _is_monday_in_tz(KOLKATA, tuesday) is False
    assert _is_monday_in_tz(KOLKATA, sunday) is False


def test_monday_visibility_uses_configured_zone_not_utc_alone():
    # Sunday 22:30 UTC = Monday 04:00 Asia/Kolkata
    sunday_utc = datetime(2026, 11, 1, 22, 30, tzinfo=ZoneInfo("UTC"))
    assert _is_monday_in_tz(KOLKATA, sunday_utc) is True
    # Same instant is still Sunday evening in New York
    assert _is_monday_in_tz(NEW_YORK, sunday_utc) is False


def test_task3_registers_exactly_one_job_per_mode_when_enabled():
    fake = MagicMock()
    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_MODES_ENABLED": "true",
            "EVENING_BRIEFING_ENABLED": "true",
            "WEEKLY_DIGEST_ENABLED": "true",
            "MONDAY_PLANNING_ENABLED": "true",
            "BRIEFING_TIMEZONE": "Asia/Kolkata",
        },
        clear=False,
    ):
        _register_smart_briefing_mode_jobs(fake)

    job_ids = [call.kwargs.get("id") for call in fake.add_job.call_args_list]
    assert job_ids == [
        "smart_briefing_evening",
        "smart_briefing_weekly",
        "smart_briefing_monday",
    ]
    assert "daily_email_briefing" not in job_ids
    assert not any(str(i).startswith("personalized_briefing_") for i in job_ids)


def test_task3_registers_nothing_when_master_flag_off():
    fake = MagicMock()
    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_MODES_ENABLED": "false",
            "EVENING_BRIEFING_ENABLED": "true",
            "WEEKLY_DIGEST_ENABLED": "true",
            "MONDAY_PLANNING_ENABLED": "true",
        },
        clear=False,
    ):
        _register_smart_briefing_mode_jobs(fake)
    fake.add_job.assert_not_called()


def test_personalized_skipped_when_task3_mode_enabled():
    """Authoritative Task 3 path wins — no duplicate personalized job ids."""
    fake = MagicMock()
    prefs = {
        "timezone": "Asia/Kolkata",
        "mode_schedules": {
            "evening": {
                "enabled": True,
                "local_time": "20:00",
                "timezone": "Asia/Kolkata",
            },
            "weekly": {
                "enabled": True,
                "local_time": "18:00",
                "timezone": "Asia/Kolkata",
                "weekday": "sun",
            },
            "monday": {
                "enabled": True,
                "local_time": "08:30",
                "timezone": "Asia/Kolkata",
                "weekday": "mon",
            },
        },
    }
    with patch.dict(
        "os.environ",
        {
            "BRIEFING_PERSONALIZATION_ENABLED": "true",
            "SMART_BRIEFING_MODES_ENABLED": "true",
            "EVENING_BRIEFING_ENABLED": "true",
            "WEEKLY_DIGEST_ENABLED": "true",
            "MONDAY_PLANNING_ENABLED": "true",
        },
        clear=False,
    ):
        with (
            patch(
                "briefing_service._resolve_briefing_account",
                return_value="user@example.com",
            ),
            patch(
                "briefing_preferences.get_preferences_for_account",
                return_value={"preferences": prefs, "source": "stored"},
            ),
        ):
            _register_personalized_briefing_jobs(fake)
    fake.add_job.assert_not_called()


def test_morning_job_id_unchanged_in_start_scheduler_source():
    import inspect
    import scheduler as sched

    src = inspect.getsource(sched.start_scheduler)
    assert 'id="daily_email_briefing"' in src
    assert "_register_smart_briefing_mode_jobs" in src
    assert "_register_personalized_briefing_jobs" in src
