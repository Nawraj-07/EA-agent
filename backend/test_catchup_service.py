"""Tests for What Did I Miss? catch-up service."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from catchup_service import (
    catchup_enabled,
    default_lookback_hours,
    generate_catchup,
    resolve_catchup_window,
)


ACCOUNT = "user@example.com"


def _email(
    *,
    email_id: str,
    sender: str,
    subject: str,
    internal_date: int,
    labels: list[str] | None = None,
    action_required: bool | None = None,
    importance: str = "medium",
    summary: str = "Summary",
    deadlines: list | None = None,
) -> dict:
    analysis = {
        "importance": importance,
        "summary": summary,
        "category": "work",
        "confidence": 0.9,
        "deadlines": deadlines or [],
    }
    if action_required is not None:
        analysis["action_required"] = action_required
    return {
        "id": email_id,
        "gmail_message_id": email_id,
        "gmail_thread_id": f"t-{email_id}",
        "sender": sender,
        "subject": subject,
        "snippet": summary,
        "body": summary,
        "internal_date": internal_date,
        "label_ids": labels or ["INBOX"],
        "email_analyses": [analysis],
    }


def test_flag_default_off():
    with patch.dict("os.environ", {"WHAT_DID_I_MISS_ENABLED": "false"}, clear=False):
        assert catchup_enabled() is False


def test_default_lookback():
    with patch.dict("os.environ", {"CATCHUP_DEFAULT_LOOKBACK_HOURS": "72"}, clear=False):
        assert default_lookback_hours() == 72


def test_first_visit_uses_default_lookback():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    with patch("db.get_dashboard_visit", return_value=None):
        window = resolve_catchup_window(
            account_email=ACCOUNT, now=now
        )
    assert window["window_source"] == "default_lookback"
    assert window["first_visit"] is True
    span = window["end"] - window["start"]
    assert timedelta(hours=71) <= span <= timedelta(hours=73)


def test_last_visit_window_not_mutated():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    last = (now - timedelta(days=3)).isoformat()
    with patch(
        "db.get_dashboard_visit",
        return_value={"last_visit_at": last, "previous_visit_at": None},
    ):
        window = resolve_catchup_window(account_email=ACCOUNT, now=now)
    assert window["window_source"] == "last_visit"
    assert abs((window["start"] - (now - timedelta(days=3))).total_seconds()) < 2


def test_manual_start_override():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    start = (now - timedelta(days=10)).isoformat()
    window = resolve_catchup_window(
        account_email=ACCOUNT, start_iso=start, now=now
    )
    assert window["window_source"] in {"manual_start", "manual_start_capped"}


def test_weekend_absence_via_last_visit():
    # Friday evening → Monday morning
    monday = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    friday = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)
    with patch(
        "db.get_dashboard_visit",
        return_value={"last_visit_at": friday.isoformat()},
    ):
        window = resolve_catchup_window(account_email=ACCOUNT, now=monday)
    assert window["start"] <= friday + timedelta(minutes=1)
    assert (window["end"] - window["start"]) >= timedelta(days=2)


def test_generate_disabled():
    with patch.dict("os.environ", {"WHAT_DID_I_MISS_ENABLED": "false"}, clear=False):
        out = generate_catchup(account_email=ACCOUNT)
    assert out["enabled"] is False


def test_generate_counts_exclude_outgoing_and_dedupe():
    now = datetime.now(timezone.utc)
    ms = int(now.timestamp() * 1000)
    rows = [
        _email(
            email_id="in1",
            sender="boss@co.com",
            subject="Please approve",
            internal_date=ms - 1000,
            action_required=True,
            importance="high",
            summary="Please approve the invoice",
        ),
        _email(
            email_id="in1",  # duplicate id/message
            sender="boss@co.com",
            subject="Please approve",
            internal_date=ms - 1000,
            action_required=True,
            importance="high",
        ),
        _email(
            email_id="out1",
            sender=ACCOUNT,
            subject="Sent ask",
            internal_date=ms - 2000,
            labels=["SENT"],
            summary="Could you confirm?",
        ),
        _email(
            email_id="info1",
            sender="news@newsletter.com",
            subject="FYI digest",
            internal_date=ms - 3000,
            action_required=False,
            importance="low",
            summary="Newsletter",
        ),
    ]
    with patch.dict(
        "os.environ",
        {
            "WHAT_DID_I_MISS_ENABLED": "true",
            "ENABLE_MANUAL_SYNC": "false",
            "OPENAI_API_KEY": "",
        },
        clear=False,
    ):
        with (
            patch("db.get_dashboard_visit", return_value=None),
            patch("db.get_catchup_cache", return_value=None),
            patch("db.put_catchup_cache", return_value={}),
            patch("db.get_sync_state", return_value={"updated_at": now.isoformat(), "backfill_status": "complete"}),
            patch("db.list_emails_for_briefing_range", return_value=rows),
            patch(
                "briefing_followup.detect_followups_for_account_sync",
                return_value={"active": [], "uncertain": [], "resolved_keys": []},
            ),
        ):
            out = generate_catchup(account_email=ACCOUNT, force_refresh=True)
    assert out["enabled"] is True
    assert out["metrics"]["emails_received"] == 2  # deduped incoming only
    assert out["metrics"]["need_attention"] >= 1
    assert out["data_freshness"]["ai_used"] is False


def test_repeated_generate_uses_cache():
    cached_payload = {"enabled": True, "metrics": {"emails_received": 9}, "cached": False}
    with patch.dict("os.environ", {"WHAT_DID_I_MISS_ENABLED": "true"}, clear=False):
        with (
            patch("db.get_dashboard_visit", return_value=None),
            patch(
                "db.get_catchup_cache",
                return_value={"payload": cached_payload, "expires_at": "2099-01-01T00:00:00+00:00"},
            ),
            patch("db.list_emails_for_briefing_range") as mock_list,
        ):
            out = generate_catchup(account_email=ACCOUNT, force_refresh=False)
    assert out["cached"] is True
    assert out["metrics"]["emails_received"] == 9
    mock_list.assert_not_called()


def test_account_required():
    with patch.dict("os.environ", {"WHAT_DID_I_MISS_ENABLED": "true"}, clear=False):
        try:
            generate_catchup(account_email="")
            assert False, "expected PermissionError"
        except PermissionError:
            pass


if __name__ == "__main__":
    test_flag_default_off()
    test_default_lookback()
    test_first_visit_uses_default_lookback()
    test_last_visit_window_not_mutated()
    test_manual_start_override()
    test_weekend_absence_via_last_visit()
    test_generate_disabled()
    test_generate_counts_exclude_outgoing_and_dedupe()
    test_repeated_generate_uses_cache()
    test_account_required()
    print("All catch-up tests passed.")
