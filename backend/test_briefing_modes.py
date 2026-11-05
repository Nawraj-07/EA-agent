"""Tests for Smart Briefing Modes 2.0 (Task 3)."""

from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from briefing_modes import (
    SUPPORTED_MODES,
    compute_mode_period,
    generate_mode_briefing_content,
    list_mode_catalog,
    mode_enabled,
    run_mode_briefing,
    smart_briefing_modes_master_enabled,
)
from briefing_structure import BriefingContent

KOLKATA = ZoneInfo("Asia/Kolkata")


def test_supported_modes():
    assert SUPPORTED_MODES == frozenset({"evening", "weekly", "monday"})


def test_flags_default_off():
    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_MODES_ENABLED": "false",
            "EVENING_BRIEFING_ENABLED": "false",
            "WEEKLY_DIGEST_ENABLED": "false",
            "MONDAY_PLANNING_ENABLED": "false",
        },
        clear=False,
    ):
        assert smart_briefing_modes_master_enabled() is False
        assert mode_enabled("evening") is False
        assert mode_enabled("weekly") is False
        assert mode_enabled("monday") is False


def test_mode_requires_master_and_specific_flag():
    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_MODES_ENABLED": "true",
            "EVENING_BRIEFING_ENABLED": "true",
            "WEEKLY_DIGEST_ENABLED": "false",
            "MONDAY_PLANNING_ENABLED": "false",
        },
        clear=False,
    ):
        assert mode_enabled("evening") is True
        assert mode_enabled("weekly") is False


def test_evening_period_local_day():
    now = datetime(2026, 11, 3, 19, 0, tzinfo=KOLKATA)
    period = compute_mode_period("evening", now=now, tz=KOLKATA)
    assert period["period_key"] == "2026-11-03"
    assert period["local_start"].date() == date(2026, 11, 3)
    assert period["local_end"].hour == 19
    assert period["start_ms"] < period["end_ms"]


def test_weekly_seven_completed_days():
    # Local Tue 3 Nov → previous 7 completed days Mon 27 Oct .. Mon 2 Nov
    now = datetime(2026, 11, 3, 12, 0, tzinfo=KOLKATA)
    period = compute_mode_period("weekly", now=now, tz=KOLKATA)
    assert period["local_start"].date() == date(2026, 10, 27)
    assert period["local_end"].date() == date(2026, 11, 3)  # exclusive end
    assert period["period_key"] == "2026-10-27_2026-11-03"
    span_days = (period["local_end"].date() - period["local_start"].date()).days
    assert span_days == 7


def test_monday_planning_week_bounds():
    # Wednesday → next Monday
    wed = datetime(2026, 11, 4, 9, 0, tzinfo=KOLKATA)  # Wed
    period = compute_mode_period("monday", now=wed, tz=KOLKATA)
    assert period["local_start"].date() == date(2026, 11, 9)  # next Monday
    assert (period["local_end"].date() - period["local_start"].date()).days == 7

    mon = datetime(2026, 11, 9, 8, 30, tzinfo=KOLKATA)
    period_mon = compute_mode_period("monday", now=mon, tz=KOLKATA)
    assert period_mon["local_start"].date() == date(2026, 11, 9)


def test_dst_safe_kolkata_no_dst():
    # Asia/Kolkata has no DST; ensure ms bounds still ordered around "transition-like" dates
    now = datetime(2026, 3, 29, 12, 0, tzinfo=KOLKATA)
    period = compute_mode_period("evening", now=now, tz=KOLKATA)
    assert period["end_ms"] > period["start_ms"]


def test_unsupported_mode_period():
    try:
        compute_mode_period("morning")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_empty_emails_generate_without_llm():
    period = compute_mode_period(
        "evening",
        now=datetime(2026, 11, 3, 19, 0, tzinfo=KOLKATA),
        tz=KOLKATA,
    )
    content = generate_mode_briefing_content([], mode="evening", period=period)
    assert isinstance(content, BriefingContent)
    assert "quiet" in content.headline.lower() or content.notes


def test_catalog_structure():
    catalog = list_mode_catalog()
    assert {row["mode"] for row in catalog} == {"evening", "weekly", "monday"}


@patch("briefing_modes.upsert_smart_briefing_mode_run")
@patch("briefing_modes.hydrate_briefing_payload")
@patch("briefing_modes.generate_mode_briefing_content")
@patch("briefing_modes.list_emails_for_briefing_range")
@patch("briefing_modes._resolve_briefing_account", return_value="gaurang@example.com")
def test_run_mode_disabled_without_force(
    _account, mock_list, mock_generate, mock_hydrate, mock_upsert
):
    with patch.dict(
        "os.environ",
        {"SMART_BRIEFING_MODES_ENABLED": "false", "EVENING_BRIEFING_ENABLED": "false"},
        clear=False,
    ):
        stats = run_mode_briefing("evening", force=False)
    assert stats["status"] == "disabled"
    mock_list.assert_not_called()
    mock_upsert.assert_not_called()


@patch("briefing_modes.upsert_smart_briefing_mode_run")
@patch("briefing_modes.hydrate_briefing_payload")
@patch("briefing_modes.generate_mode_briefing_content")
@patch("briefing_modes.list_emails_for_briefing_range")
@patch("briefing_modes._resolve_briefing_account", return_value="gaurang@example.com")
def test_run_mode_force_saves_once(
    _account, mock_list, mock_generate, mock_hydrate, mock_upsert
):
    mock_list.return_value = [
        {
            "id": "e1",
            "subject": "Hi",
            "sender": "a@b.com",
            "internal_date": 1,
            "email_analyses": [
                {"importance": "high", "action_required": True, "summary": "Act", "deadlines": []}
            ],
        }
    ]
    mock_generate.return_value = BriefingContent(
        headline="Evening",
        highlights=["Hi"],
        deadlines=[],
        action_items=["Reply"],
        notes="",
        items=[],
        meetings=[],
        upcoming_deadlines=[],
    )
    mock_hydrate.return_value = {
        "headline": "Evening",
        "items": [],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {"text": "o", "important_updates": 0, "pending_actions": 0, "meetings": 0},
        "highlights": [],
        "action_items": [],
    }
    mock_upsert.return_value = {"id": "m1"}

    stats = run_mode_briefing("evening", force=True)
    assert stats["saved"] is True
    assert stats["status"] == "saved"
    assert mock_upsert.call_count == 1
    assert mock_upsert.call_args.kwargs["mode"] == "evening"


@patch("briefing_modes.upsert_smart_briefing_mode_run")
@patch("briefing_modes.generate_mode_briefing_content", side_effect=RuntimeError("openai down"))
@patch("briefing_modes.list_emails_for_briefing_range", return_value=[])
@patch("briefing_modes._resolve_briefing_account", return_value="gaurang@example.com")
def test_generation_failure_does_not_fabricate(
    _account, _list, _generate, mock_upsert
):
    mock_upsert.return_value = {"id": "fail1"}
    stats = run_mode_briefing("weekly", force=True)
    assert stats["status"] == "generation_failed"
    assert stats["saved"] is False
    # failure marker may be upserted with status=failed
    if mock_upsert.called:
        assert mock_upsert.call_args.kwargs["status"] == "failed"
        content = mock_upsert.call_args.kwargs["content"]
        assert "failed" in content.get("headline", "").lower()


@patch("briefing_modes.upsert_smart_briefing_mode_run", side_effect=RuntimeError("PGRST205 smart_briefing_mode_runs"))
@patch("briefing_modes.hydrate_briefing_payload", return_value={"headline": "X", "items": [], "meetings": [], "upcoming_deadlines": [], "overview": {}, "highlights": [], "action_items": []})
@patch("briefing_modes.generate_mode_briefing_content")
@patch("briefing_modes.list_emails_for_briefing_range", return_value=[])
@patch("briefing_modes._resolve_briefing_account", return_value="gaurang@example.com")
def test_db_write_failure(
    _account, _list, mock_generate, _hydrate, _upsert
):
    mock_generate.return_value = BriefingContent(
        headline="Quiet",
        highlights=[],
        deadlines=[],
        action_items=[],
        notes="none",
        items=[],
        meetings=[],
        upcoming_deadlines=[],
    )
    stats = run_mode_briefing("monday", force=True)
    assert stats["status"] == "save_failed"
    assert "smart_briefing_mode_runs" in (stats.get("error") or "")


def test_duplicate_period_key_stable_for_evening():
    now = datetime(2026, 11, 3, 19, 0, tzinfo=KOLKATA)
    a = compute_mode_period("evening", now=now, tz=KOLKATA)
    b = compute_mode_period("evening", now=now.replace(hour=21), tz=KOLKATA)
    assert a["period_key"] == b["period_key"]


@patch("scheduler.BackgroundScheduler")
def test_scheduler_does_not_register_mode_jobs_when_disabled(mock_sched_cls):
    from scheduler import _register_smart_briefing_mode_jobs

    sched = MagicMock()
    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_MODES_ENABLED": "false",
            "EVENING_BRIEFING_ENABLED": "false",
            "WEEKLY_DIGEST_ENABLED": "false",
            "MONDAY_PLANNING_ENABLED": "false",
        },
        clear=False,
    ):
        _register_smart_briefing_mode_jobs(sched)
    sched.add_job.assert_not_called()


if __name__ == "__main__":
    test_supported_modes()
    test_flags_default_off()
    test_mode_requires_master_and_specific_flag()
    test_evening_period_local_day()
    test_weekly_seven_completed_days()
    test_monday_planning_week_bounds()
    test_dst_safe_kolkata_no_dst()
    test_unsupported_mode_period()
    test_empty_emails_generate_without_llm()
    test_catalog_structure()
    test_run_mode_disabled_without_force()
    test_run_mode_force_saves_once()
    test_generation_failure_does_not_fabricate()
    test_db_write_failure()
    test_duplicate_period_key_stable_for_evening()
    test_scheduler_does_not_register_mode_jobs_when_disabled()
    print("All briefing modes tests passed.")
