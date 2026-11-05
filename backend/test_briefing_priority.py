"""Unit tests for smart briefing priority scoring and integration helpers."""

from datetime import date
from unittest.mock import MagicMock, patch

from briefing_priority import (
    apply_smart_priorities_to_payload,
    build_priority_reason,
    order_emails_by_priority,
    score_email_priority,
    score_emails_priority,
    select_top_priorities,
    smart_briefing_enabled,
)
from briefing_service import generate_briefing_content, run_daily_briefing
from briefing_structure import BriefingContent

DAY = date(2026, 11, 3)


def _email(
    analysis: dict | None,
    email_id: str = "e1",
    *,
    subject: str = "Test",
    thread: str | None = None,
) -> dict:
    row = {
        "id": email_id,
        "subject": subject,
        "sender": "a@b.com",
        "email_analyses": [analysis] if analysis is not None else [],
    }
    if thread:
        row["gmail_thread_id"] = thread
    return row


def test_high_importance_actionable():
    result = score_email_priority(
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "deadlines": [],
            }
        ),
        briefing_day=DAY,
    )
    assert result["score"] == 75
    assert result["breakdown"]["importance"] == 40
    assert result["breakdown"]["action_required"] == 25
    assert result["breakdown"]["confidence"] == 10


def test_low_importance_informational():
    result = score_email_priority(
        _email(
            {
                "importance": "low",
                "action_required": False,
                "confidence": 0.5,
                "deadlines": [],
            }
        ),
        briefing_day=DAY,
    )
    assert result["score"] == 0


def test_overdue_deadline():
    result = score_email_priority(
        _email(
            {
                "importance": "medium",
                "action_required": False,
                "confidence": 0.7,
                "deadlines": ["2026-11-01"],
            }
        ),
        briefing_day=DAY,
    )
    assert result["score"] == 45
    assert result["deadline_bucket"] == "overdue"


def test_deadline_today_and_tomorrow():
    today = score_email_priority(
        _email(
            {
                "importance": "medium",
                "action_required": False,
                "confidence": 0.7,
                "deadlines": ["2026-11-03"],
            }
        ),
        briefing_day=DAY,
    )
    assert today["deadline_bucket"] == "today"
    assert today["breakdown"]["deadline"] == 20

    tomorrow = score_email_priority(
        _email(
            {
                "importance": "low",
                "action_required": False,
                "confidence": 0.5,
                "deadlines": ["Due November 4, 2026"],
            }
        ),
        briefing_day=DAY,
    )
    assert tomorrow["deadline_bucket"] == "tomorrow"
    assert tomorrow["score"] == 10


def test_missing_deadline_and_confidence():
    missing_deadline = score_email_priority(
        _email(
            {
                "importance": "high",
                "action_required": False,
                "confidence": 0.9,
                "deadlines": [],
            }
        ),
        briefing_day=DAY,
    )
    assert missing_deadline["score"] == 50
    assert missing_deadline["deadline_bucket"] is None

    missing_conf = score_email_priority(
        _email(
            {
                "importance": "high",
                "action_required": True,
                "deadlines": ["2026-11-03"],
            }
        ),
        briefing_day=DAY,
    )
    assert missing_conf["confidence"] is None
    assert missing_conf["breakdown"]["confidence"] == 0
    assert missing_conf["score"] == 85


def test_invalid_analysis_data():
    result = score_email_priority(
        {
            "id": "bad",
            "email_analyses": [
                {
                    "importance": "urgent",
                    "action_required": "maybe",
                    "confidence": "not-a-number",
                    "deadlines": ["soon", None, 12345],
                }
            ],
        },
        briefing_day=DAY,
    )
    assert result["score"] == 0
    assert result["importance"] is None


def test_duplicate_threads_deduped_in_top_priorities():
    emails = [
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "deadlines": [],
                "summary": "First in thread",
            },
            "e1",
            subject="Payment due",
            thread="t-same",
        ),
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.95,
                "deadlines": ["2026-11-03"],
                "summary": "Second in thread higher score",
            },
            "e2",
            subject="Re: Payment due",
            thread="t-same",
        ),
        _email(
            {
                "importance": "medium",
                "action_required": True,
                "confidence": 0.8,
                "deadlines": [],
            },
            "e3",
            subject="Other action",
            thread="t-other",
        ),
    ]
    top = select_top_priorities(emails, briefing_day=DAY, limit=3)
    ids = [item["source_email_id"] for item in top]
    assert ids.count("e1") + ids.count("e2") == 1
    assert "e2" in ids  # higher score wins within thread
    assert "e3" in ids
    assert all(item.get("source_email_id") for item in top)
    assert all(item.get("priority_reason") for item in top)


def test_fewer_than_three_actionable():
    emails = [
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "deadlines": [],
            },
            "only-one",
            subject="Only action",
        ),
        _email(
            {
                "importance": "low",
                "action_required": False,
                "confidence": 0.4,
                "deadlines": [],
            },
            "noise",
            subject="FYI newsletter",
        ),
    ]
    top = select_top_priorities(emails, briefing_day=DAY, limit=3)
    assert len(top) == 1
    assert top[0]["source_email_id"] == "only-one"
    assert "high importance" in build_priority_reason(
        score_email_priority(emails[0], briefing_day=DAY)
    )


def test_apply_smart_priorities_uses_existing_fields():
    emails = [
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "summary": "Pay card today",
                "deadlines": ["2026-11-03"],
            },
            "bob",
            subject="BOBCARD Payment Due",
        )
    ]
    payload = {
        "headline": "Busy day",
        "items": [
            {
                "title": "Other",
                "description": "Other",
                "priority": "medium",
                "kind": "update",
                "source_email_id": "other",
                "recommended_action": "",
            }
        ],
        "meetings": [],
        "upcoming_deadlines": [],
        "highlights": [],
        "action_items": [],
    }
    enhanced = apply_smart_priorities_to_payload(
        payload, emails, briefing_day=DAY
    )
    assert enhanced["items"][0]["source_email_id"] == "bob"
    assert enhanced["items"][0]["priority"] in {"high", "medium", "low"}
    assert "Top priorities:" in enhanced["overview"]["text"]
    assert enhanced["action_items"]
    assert "headline" in enhanced


def test_feature_flag_default_off():
    with patch.dict("os.environ", {"SMART_BRIEFING_ENABLED": "false"}, clear=False):
        assert smart_briefing_enabled() is False
    with patch.dict("os.environ", {"SMART_BRIEFING_ENABLED": "true"}, clear=False):
        assert smart_briefing_enabled() is True


@patch("briefing_service.save_briefing")
@patch("briefing_service.hydrate_briefing_payload")
@patch("briefing_service.generate_briefing_content")
@patch("briefing_service.list_emails_for_briefing_day")
@patch("briefing_service._resolve_briefing_account", return_value="gaurang@example.com")
def test_flag_disabled_skips_smart_apply(
    _account,
    mock_list,
    mock_generate,
    mock_hydrate,
    mock_save,
):
    mock_list.return_value = [
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "deadlines": [],
            },
            "e1",
        )
    ]
    mock_generate.return_value = BriefingContent(
        headline="Classic",
        highlights=[],
        deadlines=[],
        action_items=[],
        notes="",
        items=[],
        meetings=[],
        upcoming_deadlines=[],
    )
    classic_payload = {
        "headline": "Classic",
        "items": [],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {"text": "classic overview", "important_updates": 0, "pending_actions": 0, "meetings": 0},
        "highlights": [],
        "action_items": [],
    }
    mock_hydrate.return_value = dict(classic_payload)
    mock_save.return_value = {"id": "b1"}

    with patch.dict("os.environ", {"SMART_BRIEFING_ENABLED": "false"}, clear=False):
        with patch(
            "briefing_priority.apply_smart_priorities_to_payload"
        ) as mock_apply:
            stats = run_daily_briefing(DAY)
            mock_apply.assert_not_called()
            assert stats["saved"] is True
            assert stats["smart_briefing_enabled"] is False
            assert mock_generate.call_args.kwargs.get("smart") is False


@patch("briefing_service.save_briefing")
@patch("briefing_service.hydrate_briefing_payload")
@patch("briefing_service.generate_briefing_content")
@patch("briefing_service.list_emails_for_briefing_day")
@patch("briefing_service._resolve_briefing_account", return_value="gaurang@example.com")
def test_smart_generation_failure_falls_back_once(
    _account,
    mock_list,
    mock_generate,
    mock_hydrate,
    mock_save,
):
    mock_list.return_value = [
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "deadlines": [],
            },
            "e1",
            subject="Need reply",
        )
    ]
    classic = BriefingContent(
        headline="Fallback classic",
        highlights=["Need reply"],
        deadlines=[],
        action_items=["Reply"],
        notes="",
        items=[],
        meetings=[],
        upcoming_deadlines=[],
    )
    mock_generate.side_effect = [RuntimeError("openai down"), classic]
    mock_hydrate.return_value = {
        "headline": "Fallback classic",
        "items": [
            {
                "title": "Need reply",
                "description": "Please reply",
                "priority": "high",
                "kind": "action",
                "source_email_id": "e1",
                "recommended_action": "Reply",
            }
        ],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {
            "text": "overview",
            "important_updates": 1,
            "pending_actions": 1,
            "meetings": 0,
        },
        "highlights": [],
        "action_items": ["Reply"],
    }
    mock_save.return_value = {"id": "b2"}

    with patch.dict("os.environ", {"SMART_BRIEFING_ENABLED": "true"}, clear=False):
        stats = run_daily_briefing(DAY)

    assert stats["saved"] is True
    assert stats["smart_fallback"] == "generation"
    assert mock_generate.call_count == 2
    assert mock_generate.call_args_list[0].kwargs.get("smart") is True
    assert mock_generate.call_args_list[1].kwargs.get("smart") is False
    assert mock_save.call_count == 1  # no duplicate save from fallback


@patch("briefing_service.save_briefing")
@patch("briefing_service.hydrate_briefing_payload")
@patch("briefing_service.generate_briefing_content")
@patch("briefing_service.list_emails_for_briefing_day")
@patch("briefing_service._resolve_briefing_account", return_value="gaurang@example.com")
def test_smart_ranking_failure_keeps_classic_payload(
    _account,
    mock_list,
    mock_generate,
    mock_hydrate,
    mock_save,
):
    mock_list.return_value = [_email({"importance": "high", "action_required": True}, "e1")]
    mock_generate.return_value = BriefingContent(
        headline="Classic",
        highlights=[],
        deadlines=[],
        action_items=[],
        notes="",
        items=[],
        meetings=[],
        upcoming_deadlines=[],
    )
    classic_payload = {
        "headline": "Classic",
        "items": [{"title": "Keep me", "priority": "high", "kind": "action", "source_email_id": "e1"}],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {"text": "classic", "important_updates": 1, "pending_actions": 1, "meetings": 0},
        "highlights": [],
        "action_items": [],
    }
    mock_hydrate.return_value = dict(classic_payload)
    mock_save.return_value = {"id": "b3"}

    with patch.dict("os.environ", {"SMART_BRIEFING_ENABLED": "true"}, clear=False):
        with patch(
            "briefing_priority.apply_smart_priorities_to_payload",
            side_effect=RuntimeError("ranking boom"),
        ):
            stats = run_daily_briefing(DAY)

    assert stats["saved"] is True
    assert stats["smart_fallback"] == "ranking"
    saved_payload = mock_save.call_args.kwargs["content"]
    assert saved_payload["headline"] == "Classic"
    assert saved_payload["items"][0]["title"] == "Keep me"
    assert mock_save.call_count == 1


def test_order_emails_by_priority_stable():
    emails = [
        _email({"importance": "low"}, "low"),
        _email({"importance": "high", "action_required": True, "confidence": 0.9}, "high"),
    ]
    ordered = order_emails_by_priority(emails, briefing_day=DAY)
    assert [row["id"] for row in ordered] == ["high", "low"]


def test_existing_empty_generate_unchanged():
    content = generate_briefing_content([])
    assert content.headline == "Quiet inbox"


def test_api_payload_keeps_required_keys():
    emails = [
        _email(
            {
                "importance": "high",
                "action_required": True,
                "confidence": 0.9,
                "deadlines": ["2026-11-03"],
                "summary": "Act now",
            },
            "e1",
            subject="Act",
        )
    ]
    payload = apply_smart_priorities_to_payload(
        {
            "headline": "Day",
            "items": [],
            "meetings": [],
            "upcoming_deadlines": [{"title": "X", "due_date": "2026-11-03"}],
            "highlights": ["h"],
            "action_items": [],
            "notes": "",
            "deadlines": [],
        },
        emails,
        briefing_day=DAY,
    )
    for key in (
        "headline",
        "items",
        "meetings",
        "upcoming_deadlines",
        "overview",
        "highlights",
        "action_items",
    ):
        assert key in payload
    assert payload["items"][0]["priority"] in {"high", "medium", "low"}


if __name__ == "__main__":
    test_high_importance_actionable()
    test_low_importance_informational()
    test_overdue_deadline()
    test_deadline_today_and_tomorrow()
    test_missing_deadline_and_confidence()
    test_invalid_analysis_data()
    test_duplicate_threads_deduped_in_top_priorities()
    test_fewer_than_three_actionable()
    test_apply_smart_priorities_uses_existing_fields()
    test_feature_flag_default_off()
    test_flag_disabled_skips_smart_apply()
    test_smart_generation_failure_falls_back_once()
    test_smart_ranking_failure_keeps_classic_payload()
    test_order_emails_by_priority_stable()
    test_existing_empty_generate_unchanged()
    test_api_payload_keeps_required_keys()
    print("All briefing priority tests passed.")
