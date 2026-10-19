
"""Unit tests for briefing history date bucketing (no OpenAI / DB)."""

from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from briefing_service import (
    build_briefing_history,
    date_iso_to_internal_ms_range,
    history_date_range,
    local_date_for,
    parse_target_briefing_date,
    run_daily_briefing,
)


TZ = ZoneInfo("Asia/Kolkata")


def test_history_date_range_is_eight_days_oldest_to_newest():
    today = date(2026, 10, 19)
    dates = history_date_range(today)
    assert len(dates) == 8
    assert dates[0] == date(2026, 10, 12)
    assert dates[-1] == date(2026, 10, 19)


def test_missing_dates_return_empty_slots():
    today = date(2026, 10, 19)
    dates = history_date_range(today)
    rows = [
        {
            "id": "b1",
            "created_at": "2026-10-19T05:00:00+00:00",
            "content": {"headline": "Today"},
        }
    ]
    days = build_briefing_history(rows, dates=dates, tz=TZ)
    assert len(days) == 8
    empty = [d for d in days if not d["has_briefing"]]
    filled = [d for d in days if d["has_briefing"]]
    assert len(empty) == 7
    assert len(filled) == 1
    assert filled[0]["date"] == "2026-10-19"
    assert filled[0]["briefing"]["id"] == "b1"
    assert all(d["briefing"] is None for d in empty)


def test_multiple_briefings_same_day_keeps_latest():
    today = date(2026, 10, 19)
    dates = history_date_range(today)
    rows = [
        {
            "id": "older",
            "created_at": "2026-10-19T04:00:00+00:00",
            "content": {"headline": "Morning"},
        },
        {
            "id": "newer",
            "created_at": "2026-10-19T10:00:00+00:00",
            "content": {"headline": "Afternoon"},
        },
    ]
    days = build_briefing_history(rows, dates=dates, tz=TZ)
    slot = next(d for d in days if d["date"] == "2026-10-19")
    assert slot["has_briefing"] is True
    assert slot["briefing"]["id"] == "newer"


def test_local_midnight_boundary_asia_kolkata():
    # 2026-10-18 18:30 UTC == 2026-10-19 00:00 IST
    just_after = datetime(2026, 10, 18, 18, 30, tzinfo=timezone.utc)
    just_before = datetime(2026, 10, 18, 18, 29, tzinfo=timezone.utc)
    assert local_date_for(just_after, TZ) == date(2026, 10, 19)
    assert local_date_for(just_before, TZ) == date(2026, 10, 18)


def test_date_iso_to_internal_ms_range_inclusive_kolkata():
    start_ms, end_ms = date_iso_to_internal_ms_range(
        "2026-10-12", "2026-10-19", tz=TZ
    )
    assert start_ms == int(
        datetime(2026, 10, 12, 0, 0, tzinfo=TZ).timestamp() * 1000
    )
    # Exclusive end = midnight start of Oct 20 IST
    assert end_ms == int(
        datetime(2026, 10, 20, 0, 0, tzinfo=TZ).timestamp() * 1000
    )
    # Message at Oct 19 23:59 IST is inside; Oct 20 00:00 is outside
    inside = int(datetime(2026, 10, 19, 23, 59, tzinfo=TZ).timestamp() * 1000)
    assert start_ms <= inside < end_ms
    boundary = end_ms
    assert not (start_ms <= boundary < end_ms)


def test_date_iso_to_internal_ms_range_month_year_rollover():
    start_ms, end_ms = date_iso_to_internal_ms_range(
        "2025-12-28", "2026-01-04", tz=TZ
    )
    assert start_ms is not None and end_ms is not None
    assert end_ms > start_ms
    jan1 = int(datetime(2026, 1, 1, 12, 0, tzinfo=TZ).timestamp() * 1000)
    assert start_ms <= jan1 < end_ms

    dates = history_date_range(date(2026, 10, 19))
    rows = [
        {
            "id": "boundary",
            "created_at": "2026-10-18T18:30:00+00:00",
            "content": {},
        }
    ]
    days = build_briefing_history(rows, dates=dates, tz=TZ)
    assert next(d for d in days if d["date"] == "2026-10-19")["briefing"]["id"] == "boundary"
    assert next(d for d in days if d["date"] == "2026-10-18")["has_briefing"] is False


def test_build_history_does_not_require_openai():
    # Smoke: building history with empty rows needs no LLM imports beyond module load.
    days = build_briefing_history([], dates=history_date_range(date(2026, 1, 8)), tz=TZ)
    assert len(days) == 8
    assert all(d["briefing"] is None for d in days)


def test_briefing_date_column_overrides_created_at():
    today = date(2026, 10, 19)
    dates = history_date_range(today)
    rows = [
        {
            "id": "for-12",
            "briefing_date": "2026-10-12",
            "created_at": "2026-10-19T10:00:00+00:00",
            "content": {"headline": "Past day"},
        }
    ]
    days = build_briefing_history(rows, dates=dates, tz=TZ)
    assert next(d for d in days if d["date"] == "2026-10-12")["briefing"]["id"] == "for-12"
    assert next(d for d in days if d["date"] == "2026-10-19")["has_briefing"] is False


def test_parse_target_rejects_outside_window():
    try:
        parse_target_briefing_date("2000-01-01")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "within today" in str(exc).lower() or "previous" in str(exc).lower()


def test_run_daily_briefing_saves_requested_briefing_date():
    captured = {}

    class FakeContent:
        headline = "Past"
        highlights = []
        deadlines = []
        action_items = []
        notes = ""

    with (
        patch(
            "briefing_service.parse_target_briefing_date",
            return_value=date(2026, 10, 12),
        ),
        patch(
            "briefing_service._resolve_briefing_account",
            return_value="me@example.com",
        ),
        patch(
            "briefing_service.list_emails_for_briefing_day",
            return_value=[],
        ),
        patch(
            "briefing_service.generate_briefing_content",
            return_value=FakeContent(),
        ),
        patch(
            "briefing_service.save_briefing",
            side_effect=lambda **kwargs: captured.update(kwargs) or {"id": "saved-1"},
        ),
    ):
        stats = run_daily_briefing(target_date="2026-10-12")

    assert stats["status"] == "saved"
    assert stats["briefing_date"] == "2026-10-12"
    assert captured["briefing_date"] == "2026-10-12"
    assert captured["gmail_account_email"] == "me@example.com"


if __name__ == "__main__":
    test_history_date_range_is_eight_days_oldest_to_newest()
    test_missing_dates_return_empty_slots()
    test_multiple_briefings_same_day_keeps_latest()
    test_local_midnight_boundary_asia_kolkata()
    test_date_iso_to_internal_ms_range_inclusive_kolkata()
    test_date_iso_to_internal_ms_range_month_year_rollover()
    test_build_history_does_not_require_openai()
    test_briefing_date_column_overrides_created_at()
    test_run_daily_briefing_saves_requested_briefing_date()
    print("All briefing history tests passed.")
