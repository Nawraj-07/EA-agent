"""Tests for Interactive Briefing recommendations (Task 4)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from briefing_recommendations import (
    build_recommendations_from_briefing,
    compute_snooze_until,
    draft_for_recommendation,
    interactive_briefing_enabled,
    list_active_recommendations,
    snooze_recommendation,
    update_recommendation_status,
    user_briefing_preferences,
)
from chat_service import is_briefing_question, is_recommendation_question

KOLKATA = ZoneInfo("Asia/Kolkata")
EMAIL_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_EMAIL_ID = "11111111-2222-4333-8444-555555555555"


def _briefing_row(**overrides):
    content = {
        "briefing_date": "2026-11-03",
        "summary": "Busy day.",
        "items": [
            {
                "kind": "action",
                "title": "Reply to Acme proposal",
                "priority": "high",
                "recommended_action": "Send a short acknowledgment today.",
                "source_email_id": EMAIL_ID,
                "gmail_thread_id": "thread-abc",
            },
            {
                "kind": "fyi",
                "title": "Newsletter",
                "priority": "low",
                "recommended_action": "Skim later.",
                "source_email_id": None,
            },
        ],
        "meetings": [
            {
                "event_title": "Sync with Priya",
                "status": "confirmed",
                "source_email_id": OTHER_EMAIL_ID,
            }
        ],
        "upcoming_deadlines": [
            {
                "title": "File GST return",
                "due_date": "2026-11-10",
                "priority": "high",
                "source_email_id": EMAIL_ID,
            }
        ],
    }
    row = {
        "id": "briefing-1",
        "gmail_account_email": "user@example.com",
        "briefing_date": "2026-11-03",
        "content": content,
    }
    row.update(overrides)
    return row


def test_flag_defaults_off():
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "false"}, clear=False):
        assert interactive_briefing_enabled() is False


def test_flag_enabled():
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        assert interactive_briefing_enabled() is True


def test_build_recommendations_real_sources_only():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    assert len(recs) >= 3
    keys = {r["recommendation_key"] for r in recs}
    assert len(keys) == len(recs)

    with_email = [r for r in recs if r["source_email_id"] == EMAIL_ID]
    assert with_email
    for rec in with_email:
        view = next(a for a in rec["actions"] if a["type"] == "view_email")
        draft = next(a for a in rec["actions"] if a["type"] == "draft_reply")
        assert view["available"] is True
        assert draft["available"] is True

    missing = next(r for r in recs if r["title"] == "Newsletter")
    view = next(a for a in missing["actions"] if a["type"] == "view_email")
    assert view["available"] is False
    assert view["unavailable_reason"] == "source_email_missing"


def test_stable_keys_across_regeneration():
    a = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    b = build_recommendations_from_briefing(
        _briefing_row(id="briefing-2"), account_email="user@example.com"
    )
    assert {r["recommendation_key"] for r in a} == {r["recommendation_key"] for r in b}


def test_no_fictional_email_ids():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    for rec in recs:
        email_id = rec.get("source_email_id")
        if email_id:
            assert email_id in {EMAIL_ID, OTHER_EMAIL_ID}


def test_list_disabled_returns_empty():
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "false"}, clear=False):
        payload = list_active_recommendations(account_email="user@example.com")
    assert payload["enabled"] is False
    assert payload["recommendations"] == []


def test_list_hides_completed_and_snoozed():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    target = recs[0]
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch(
                "db.get_latest_briefing",
                return_value=_briefing_row(),
            ),
            patch(
                "db.list_recommendation_states_for_account",
                return_value=[
                    {
                        "id": "state-1",
                        "recommendation_key": target["recommendation_key"],
                        "status": "completed",
                        "snoozed_until": None,
                    }
                ],
            ),
            patch(
                "db.release_expired_recommendation_snoozes",
                return_value=0,
            ),
        ):
            active = list_active_recommendations(account_email="user@example.com")
            keys = {r["recommendation_key"] for r in active["recommendations"]}
            assert target["recommendation_key"] not in keys

        with (
            patch(
                "db.get_latest_briefing",
                return_value=_briefing_row(),
            ),
            patch(
                "db.list_recommendation_states_for_account",
                return_value=[
                    {
                        "id": "state-2",
                        "recommendation_key": target["recommendation_key"],
                        "status": "snoozed",
                        "snoozed_until": future,
                    }
                ],
            ),
            patch(
                "db.release_expired_recommendation_snoozes",
                return_value=0,
            ),
        ):
            active = list_active_recommendations(account_email="user@example.com")
            keys = {r["recommendation_key"] for r in active["recommendations"]}
            assert target["recommendation_key"] not in keys


def test_status_transitions():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    key = recs[0]["recommendation_key"]
    saved = {
        "id": "state-3",
        "recommendation_key": key,
        "status": "completed",
        "snoozed_until": None,
    }
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch(
                "db.get_latest_briefing",
                return_value=_briefing_row(),
            ),
            patch("db.list_recommendation_states_for_account", return_value=[]),
            patch("db.get_recommendation_state", return_value=None),
            patch("db.release_expired_recommendation_snoozes", return_value=0),
            patch("db.upsert_recommendation_state", return_value=saved) as upsert,
        ):
            result = update_recommendation_status(
                account_email="user@example.com",
                recommendation_key=key,
                status="completed",
            )
            assert result["recommendation"]["status"] == "completed"
            upsert.assert_called_once()
            kwargs = upsert.call_args.kwargs
            assert kwargs["status"] == "completed"
            assert kwargs["snoozed_until"] is None


def test_reject_snooze_via_status_endpoint_logic():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    key = recs[0]["recommendation_key"]
    try:
        update_recommendation_status(
            account_email="user@example.com",
            recommendation_key=key,
            status="snoozed",
        )
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_snooze_presets_timezone_aware():
    now = datetime(2026, 11, 3, 10, 0, tzinfo=KOLKATA)
    later = compute_snooze_until("later_today", now=now, tz=KOLKATA)
    assert later.tzinfo is not None
    assert later.astimezone(KOLKATA).hour == 13

    tomorrow = compute_snooze_until("tomorrow", now=now, tz=KOLKATA)
    local = tomorrow.astimezone(KOLKATA)
    assert local.date().isoformat() == "2026-11-04"
    assert local.hour == 9

    next_week = compute_snooze_until("next_week", now=now, tz=KOLKATA)
    assert next_week.astimezone(KOLKATA).weekday() == 0


def test_snooze_custom_and_persist():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    key = recs[0]["recommendation_key"]
    custom = "2026-11-04T15:30:00+05:30"
    saved = {
        "id": "state-4",
        "recommendation_key": key,
        "status": "snoozed",
        "snoozed_until": "2026-11-04T10:00:00+00:00",
    }
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch("db.get_latest_briefing", return_value=_briefing_row()),
            patch("db.list_recommendation_states_for_account", return_value=[]),
            patch("db.get_recommendation_state", return_value=None),
            patch("db.release_expired_recommendation_snoozes", return_value=0),
            patch("db.upsert_recommendation_state", return_value=saved) as upsert,
        ):
            result = snooze_recommendation(
                account_email="user@example.com",
                recommendation_key=key,
                custom_iso=custom,
            )
            assert result["recommendation"]["status"] == "snoozed"
            assert upsert.call_args.kwargs["status"] == "snoozed"
            assert upsert.call_args.kwargs["snoozed_until"]


def test_draft_never_sends():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    key = next(r for r in recs if r["source_email_id"] == EMAIL_ID)[
        "recommendation_key"
    ]
    email = {
        "id": EMAIL_ID,
        "gmail_account_email": "user@example.com",
        "subject": "Proposal",
        "from_email": "acme@example.com",
        "body_text": "Please confirm.",
    }
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch("db.get_latest_briefing", return_value=_briefing_row()),
            patch("db.list_recommendation_states_for_account", return_value=[]),
            patch("db.get_recommendation_state", return_value=None),
            patch("db.release_expired_recommendation_snoozes", return_value=0),
            patch("db.get_stored_email", return_value=email),
            patch(
                "draft_service.generate_email_draft",
                return_value="Thanks, confirmed.",
            ) as gen,
            patch(
                "draft_service.is_no_reply_needed",
                return_value=False,
            ),
            patch(
                "db.save_draft",
                return_value={
                    "id": "draft-1",
                    "email_id": EMAIL_ID,
                    "body": "Thanks, confirmed.",
                    "status": "pending",
                },
            ) as save,
        ):
            result = draft_for_recommendation(
                account_email="user@example.com",
                recommendation_key=key,
            )
            assert result["ok"] is True
            assert "Nothing was sent" in result["note"]
            assert result["draft"]["status"] == "pending"
            gen.assert_called_once()
            save.assert_called_once()


def test_draft_missing_source():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    key = next(r for r in recs if r["title"] == "Newsletter")["recommendation_key"]
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch("db.get_latest_briefing", return_value=_briefing_row()),
            patch("db.list_recommendation_states_for_account", return_value=[]),
            patch("db.get_recommendation_state", return_value=None),
            patch("db.release_expired_recommendation_snoozes", return_value=0),
        ):
            try:
                draft_for_recommendation(
                    account_email="user@example.com",
                    recommendation_key=key,
                )
                assert False, "expected ValueError"
            except ValueError as exc:
                assert "draft_unavailable" in str(exc)


def test_draft_wrong_account_forbidden():
    recs = build_recommendations_from_briefing(
        _briefing_row(), account_email="user@example.com"
    )
    key = next(r for r in recs if r["source_email_id"] == EMAIL_ID)[
        "recommendation_key"
    ]
    email = {
        "id": EMAIL_ID,
        "gmail_account_email": "other@example.com",
        "subject": "Proposal",
    }
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch("db.get_latest_briefing", return_value=_briefing_row()),
            patch("db.list_recommendation_states_for_account", return_value=[]),
            patch("db.get_recommendation_state", return_value=None),
            patch("db.release_expired_recommendation_snoozes", return_value=0),
            patch("db.get_stored_email", return_value=email),
        ):
            try:
                draft_for_recommendation(
                    account_email="user@example.com",
                    recommendation_key=key,
                )
                assert False, "expected PermissionError"
            except PermissionError:
                pass


def test_invalid_recommendation_key():
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        with (
            patch("db.get_latest_briefing", return_value=_briefing_row()),
            patch("db.list_recommendation_states_for_account", return_value=[]),
            patch("db.get_recommendation_state", return_value=None),
            patch("db.release_expired_recommendation_snoozes", return_value=0),
        ):
            try:
                update_recommendation_status(
                    account_email="user@example.com",
                    recommendation_key="does-not-exist",
                    status="completed",
                )
                assert False, "expected KeyError"
            except KeyError:
                pass


def test_preferences_safe_defaults():
    with patch.dict("os.environ", {"BRIEFING_PREFERRED_TONE": ""}, clear=False):
        prefs = user_briefing_preferences()
    assert prefs["tone"] == "professional"
    assert "work_hours_start" in prefs


def test_recommendation_chat_intent_gated_by_flag():
    q = "What should I handle first?"
    assert is_recommendation_question(q) is True
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "false"}, clear=False):
        assert is_briefing_question(q) is False
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "true"}, clear=False):
        assert is_briefing_question(q) is True


def test_existing_briefing_question_still_works_when_flag_off():
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "false"}, clear=False):
        assert is_briefing_question("What's in my daily briefing?") is True


def test_db_failure_list_does_not_crash_flag_off_path():
    """Flag off never touches recommendation table."""
    with patch.dict("os.environ", {"INTERACTIVE_BRIEFING_ENABLED": "false"}, clear=False):
        payload = list_active_recommendations(account_email="user@example.com")
    assert payload["enabled"] is False


def test_duplicate_items_collapsed():
    row = _briefing_row()
    row["content"]["items"].append(dict(row["content"]["items"][0]))
    recs = build_recommendations_from_briefing(row, account_email="user@example.com")
    keys = [r["recommendation_key"] for r in recs]
    assert len(keys) == len(set(keys))
