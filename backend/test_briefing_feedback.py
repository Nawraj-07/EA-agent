"""Tests for Task 6 — feedback, adaptive ranking, validation, metrics."""

from datetime import date
from unittest.mock import patch

from briefing_feedback import (
    FEEDBACK_TYPES,
    adaptive_ranking_enabled,
    aggregate_feedback_signals,
    apply_feedback_adjustment,
    bound_feedback_delta,
    feedback_delta_for_email,
    feedback_enabled,
    filter_validated_recommendations,
    submit_recommendation_feedback,
    validate_recommendation_candidate,
)
from briefing_priority import score_email_priority


def test_feedback_flags_default_off():
    with patch.dict(
        "os.environ",
        {
            "BRIEFING_FEEDBACK_ENABLED": "false",
            "BRIEFING_ADAPTIVE_RANKING_ENABLED": "false",
        },
    ):
        assert feedback_enabled() is False
        assert adaptive_ranking_enabled() is False


def test_invalid_feedback_type_rejected():
    with patch.dict("os.environ", {"BRIEFING_FEEDBACK_ENABLED": "true"}):
        try:
            submit_recommendation_feedback(
                account_email="user@example.com",
                recommendation_key="abc",
                feedback_type="awesome",
            )
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_feedback_disabled_raises():
    with patch.dict("os.environ", {"BRIEFING_FEEDBACK_ENABLED": "false"}):
        try:
            submit_recommendation_feedback(
                account_email="user@example.com",
                recommendation_key="abc",
                feedback_type="useful",
            )
            assert False, "expected PermissionError"
        except PermissionError:
            pass


def test_feedback_upsert_safe_repeat():
    saved = {
        "id": "fb-1",
        "recommendation_key": "key-1",
        "feedback_type": "useful",
        "updated_at": "2026-11-03T00:00:00Z",
    }
    with patch.dict("os.environ", {"BRIEFING_FEEDBACK_ENABLED": "true"}):
        with (
            patch(
                "briefing_recommendations.get_owned_recommendation",
                return_value={
                    "recommendation_key": "key-1",
                    "source_email_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    "title": "Reply Acme",
                },
            ),
            patch("db.upsert_recommendation_feedback", return_value=saved) as upsert,
        ):
            first = submit_recommendation_feedback(
                account_email="user@example.com",
                recommendation_key="key-1",
                feedback_type="useful",
            )
            second = submit_recommendation_feedback(
                account_email="user@example.com",
                recommendation_key="key-1",
                feedback_type="not_useful",
            )
            assert first["feedback"]["feedback_type"] == "useful"
            assert upsert.call_count == 2
            assert second["feedback"]["recommendation_key"] == "key-1"


def test_unauthorized_recommendation_feedback():
    with patch.dict("os.environ", {"BRIEFING_FEEDBACK_ENABLED": "true"}):
        with (
            patch(
                "briefing_recommendations.get_owned_recommendation",
                return_value=None,
            ),
            patch("db.get_recommendation_state", return_value=None),
        ):
            try:
                submit_recommendation_feedback(
                    account_email="user@example.com",
                    recommendation_key="missing",
                    feedback_type="useful",
                )
                assert False, "expected KeyError"
            except KeyError:
                pass


def test_adaptive_ranking_bounded():
    assert bound_feedback_delta(100) == 12
    assert bound_feedback_delta(-100) == -12
    adjusted, delta = apply_feedback_adjustment(50, feedback_delta=-100, urgent=False)
    assert adjusted >= 0
    assert abs(delta) <= 12


def test_urgent_not_fully_suppressed_by_negative_feedback():
    adjusted, _delta = apply_feedback_adjustment(
        55, feedback_delta=-100, urgent=True
    )
    assert adjusted >= 40


def test_domain_feedback_signals_and_delta():
    rows = [
        {
            "recommendation_key": "k1",
            "feedback_type": "useful",
            "metadata": {"sender_email": "a@client.com"},
        },
        {
            "recommendation_key": "k2",
            "feedback_type": "irrelevant",
            "metadata": {"sender_email": "n@newsletter.com"},
        },
    ]
    signals = aggregate_feedback_signals(rows)
    assert signals["type_counts"]["useful"] == 1
    assert signals["type_counts"]["irrelevant"] == 1
    with patch.dict("os.environ", {"BRIEFING_ADAPTIVE_RANKING_ENABLED": "true"}):
        boost = feedback_delta_for_email(
            signals=signals, sender_email="b@client.com"
        )
        cut = feedback_delta_for_email(
            signals=signals, sender_email="x@newsletter.com"
        )
    assert boost > 0
    assert cut < 0


def test_score_integrates_feedback_without_second_ranker():
    email = {
        "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "sender": "n@newsletter.com",
        "from_email": "n@newsletter.com",
        "subject": "Deal",
        "email_analyses": [
            {"importance": "medium", "action_required": True, "confidence": 0.9}
        ],
    }
    day = date(2026, 11, 3)
    base = score_email_priority(email, briefing_day=day)
    lowered = score_email_priority(
        email, briefing_day=day, feedback_delta=-10
    )
    assert lowered["score"] < base["score"]
    assert lowered["score"] >= 0


def test_duplicate_recommendations_prevented():
    cand = {
        "title": "Reply Acme",
        "source_email_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "recommendation_key": "same-key",
        "priority_score": 50,
        "kind": "action",
    }
    with (
        patch("db.bump_quality_counter", return_value={}),
        patch("db.get_quality_counters", return_value=None),
    ):
        kept, stats = filter_validated_recommendations(
            [cand, dict(cand)],
            account_email="user@example.com",
            known_email_ids={"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        )
    assert len(kept) == 1
    assert stats["duplicates_prevented"] == 1


def test_missing_source_reference_handled():
    cand = {
        "title": "Ghost item",
        "source_email_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "priority_score": 40,
    }
    ok, reason = validate_recommendation_candidate(
        cand,
        account_email="user@example.com",
        known_email_ids=set(),
    )
    assert ok is False
    assert reason == "source_email_missing"


def test_material_change_allows_new_key():
    a = {
        "title": "Reply Acme — v1",
        "source_email_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "recommendation_key": "key-a",
        "priority_score": 50,
    }
    b = {
        "title": "Reply Acme — urgent update",
        "source_email_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "recommendation_key": "key-b",
        "priority_score": 70,
    }
    with patch("db.bump_quality_counter", return_value={}):
        kept, stats = filter_validated_recommendations(
            [a, b],
            account_email="user@example.com",
            known_email_ids={"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        )
    assert len(kept) == 2
    assert stats["duplicates_prevented"] == 0


def test_feedback_types_complete():
    assert FEEDBACK_TYPES == {
        "useful",
        "not_useful",
        "incorrect_priority",
        "already_handled",
        "irrelevant",
    }


def test_metrics_summary_structure():
    with patch.dict(
        "os.environ",
        {
            "BRIEFING_FEEDBACK_ENABLED": "true",
            "BRIEFING_ADAPTIVE_RANKING_ENABLED": "false",
        },
    ):
        with (
            patch(
                "db.list_recommendation_feedback_for_account",
                return_value=[
                    {"recommendation_key": "k", "feedback_type": "useful", "metadata": {}}
                ],
            ),
            patch(
                "db.list_recommendation_states_for_account",
                return_value=[{"status": "completed"}],
            ),
            patch(
                "db.get_quality_counters",
                return_value={
                    "metrics": {
                        "recommendations_generated": 3,
                        "duplicates_prevented": 1,
                        "validation_failures": 0,
                    }
                },
            ),
        ):
            from briefing_feedback import feedback_summary_for_account

            summary = feedback_summary_for_account("user@example.com")
    assert summary["feedback_counts"]["useful"] == 1
    assert summary["recommendation_status_counts"]["completed"] == 1
    assert summary["quality"]["duplicates_prevented"] == 1
    assert "not verified" in summary["note"].lower() or "acknowledged" in summary["note"].lower()
