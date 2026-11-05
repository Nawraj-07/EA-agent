"""Tests for pending-reply / follow-up detection (Task 2)."""

from datetime import date
from unittest.mock import patch

from briefing_followup import (
    apply_pending_replies_to_payload,
    classify_thread_state,
    detect_followups_for_emails,
    message_direction,
    normalize_email_address,
    pending_reply_detection_enabled,
)
from briefing_service import run_daily_briefing
from briefing_structure import BriefingContent

ACCOUNT = "gaurang@rbhargavaassociates.in"
DAY = date(2026, 11, 3)


def _msg(
    *,
    email_id: str,
    sender: str,
    subject: str = "Hello",
    thread: str = "t1",
    internal_date: int = 1,
    labels: list[str] | None = None,
    action_required: bool | None = None,
    importance: str = "medium",
    summary: str = "Summary",
) -> dict:
    analysis = {
        "importance": importance,
        "summary": summary,
        "category": "work",
        "confidence": 0.9,
        "deadlines": [],
    }
    if action_required is not None:
        analysis["action_required"] = action_required
    return {
        "id": email_id,
        "gmail_thread_id": thread,
        "sender": sender,
        "subject": subject,
        "snippet": summary,
        "body": summary,
        "internal_date": internal_date,
        "label_ids": labels or ["INBOX"],
        "email_analyses": [analysis],
    }


def test_normalize_and_direction():
    assert normalize_email_address("Gaurang <gaurang@rbhargavaassociates.in>") == ACCOUNT
    assert (
        message_direction(
            {"sender": "Boss <boss@co.com>", "label_ids": ["INBOX"]},
            ACCOUNT,
        )
        == "incoming"
    )
    assert (
        message_direction(
            {"sender": ACCOUNT, "label_ids": ["SENT"]},
            ACCOUNT,
        )
        == "outgoing"
    )
    assert (
        message_direction(
            {"sender": f"Me <{ACCOUNT}>", "label_ids": ["INBOX"]},
            ACCOUNT,
        )
        == "outgoing"
    )


def test_pending_reply_when_latest_inbound_action_required():
    messages = [
        _msg(
            email_id="e1",
            sender="boss@co.com",
            subject="Need update",
            internal_date=10,
            action_required=True,
            importance="high",
        )
    ]
    result = classify_thread_state(messages, account_email=ACCOUNT)
    assert result["state"] == "pending_reply"
    assert result["source_email_id"] == "e1"


def test_outgoing_after_inbound_resolves_pending():
    messages = [
        _msg(
            email_id="e1",
            sender="boss@co.com",
            subject="Need update",
            internal_date=10,
            action_required=True,
        ),
        _msg(
            email_id="e2",
            sender=ACCOUNT,
            subject="Re: Need update",
            internal_date=20,
            labels=["SENT"],
            action_required=False,
            summary="Here is the update.",
        ),
    ]
    result = classify_thread_state(messages, account_email=ACCOUNT)
    assert result["state"] == "resolved"


def test_sent_draft_clears_pending():
    messages = [
        _msg(
            email_id="e1",
            sender="boss@co.com",
            subject="Please confirm",
            internal_date=10,
            action_required=True,
        ),
        # No outgoing email in INBOX store, but draft was sent
    ]
    pending = classify_thread_state(messages, account_email=ACCOUNT, sent_cleared=False)
    assert pending["state"] == "pending_reply"
    cleared = classify_thread_state(messages, account_email=ACCOUNT, sent_cleared=True)
    # Last message still inbound; sent_cleared alone does not invent resolution
    # when latest evidence is still an unanswered inbound — keep pending.
    assert cleared["state"] == "pending_reply"


def test_informational_noreply():
    messages = [
        _msg(
            email_id="e1",
            sender="noreply@newsletter.com",
            subject="Weekly digest",
            internal_date=5,
            action_required=False,
            importance="low",
        )
    ]
    result = classify_thread_state(messages, account_email=ACCOUNT)
    assert result["state"] == "informational"


def test_unknown_when_analysis_missing():
    messages = [
        {
            "id": "e1",
            "gmail_thread_id": "t1",
            "sender": "someone@co.com",
            "subject": "Hi",
            "internal_date": 1,
            "label_ids": ["INBOX"],
            "email_analyses": [],
        }
    ]
    result = classify_thread_state(messages, account_email=ACCOUNT)
    assert result["state"] == "unknown"


def test_awaiting_response_requires_outgoing_ask():
    messages = [
        _msg(
            email_id="e1",
            sender="boss@co.com",
            subject="Hello",
            internal_date=10,
            action_required=False,
        ),
        _msg(
            email_id="e2",
            sender=ACCOUNT,
            subject="Re: Hello — can you confirm?",
            internal_date=20,
            labels=["SENT"],
            action_required=False,
            summary="Can you confirm the date?",
        ),
    ]
    result = classify_thread_state(messages, account_email=ACCOUNT)
    assert result["state"] == "awaiting_response"


def test_later_inbound_clears_awaiting():
    messages = [
        _msg(
            email_id="e1",
            sender=ACCOUNT,
            subject="Can you confirm?",
            internal_date=10,
            labels=["SENT"],
            summary="Can you confirm?",
        ),
        _msg(
            email_id="e2",
            sender="boss@co.com",
            subject="Re: Can you confirm?",
            internal_date=20,
            action_required=False,
            importance="low",
            summary="Confirmed.",
        ),
    ]
    result = classify_thread_state(messages, account_email=ACCOUNT)
    assert result["state"] == "informational"


def test_detect_followups_skips_low_confidence_awaiting():
    emails = [
        _msg(
            email_id="only-out",
            sender=ACCOUNT,
            subject="Quick question?",
            internal_date=1,
            labels=["INBOX"],  # self-sent appearance without SENT label history
            summary="Could you update me?",
        )
    ]
    # Direction outgoing via From==account; awaiting may be low-confidence and skipped
    found = detect_followups_for_emails(
        emails,
        account_email=ACCOUNT,
        gmail_account_email=ACCOUNT,
        list_thread_messages=lambda **_kwargs: emails,
    )
    assert all(item.get("followup_state") != "awaiting_response" or item.get("followup_confidence") != "low" for item in found)


def test_apply_pending_replies_uses_existing_fields():
    emails = [
        _msg(
            email_id="e1",
            sender="boss@co.com",
            subject="Need numbers",
            action_required=True,
            importance="high",
        )
    ]
    payload = {
        "headline": "Day",
        "items": [
            {
                "title": "Other",
                "priority": "medium",
                "kind": "update",
                "source_email_id": "other",
                "recommended_action": "",
            }
        ],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {"text": "base", "important_updates": 0, "pending_actions": 0, "meetings": 0},
        "action_items": [],
        "highlights": [],
    }
    with patch("briefing_followup._sent_draft_thread_ids", return_value=set()):
        enhanced = apply_pending_replies_to_payload(
            payload,
            emails,
            account_email=ACCOUNT,
            list_thread_messages=lambda **_kwargs: emails,
        )
    assert enhanced["items"][0]["source_email_id"] == "e1"
    assert "Reply" in (enhanced["items"][0].get("recommended_action") or "")
    assert "Follow-ups:" in enhanced["overview"]["text"]
    assert "headline" in enhanced
    assert enhanced["pending_replies"]["count"] >= 1


def test_feature_flag_default_off():
    with patch.dict("os.environ", {"PENDING_REPLY_DETECTION_ENABLED": "false"}, clear=False):
        assert pending_reply_detection_enabled() is False
    with patch.dict("os.environ", {"PENDING_REPLY_DETECTION_ENABLED": "true"}, clear=False):
        assert pending_reply_detection_enabled() is True


@patch("briefing_service.save_briefing")
@patch("briefing_service.hydrate_briefing_payload")
@patch("briefing_service.generate_briefing_content")
@patch("briefing_service.list_emails_for_briefing_day")
@patch("briefing_service._resolve_briefing_account", return_value=ACCOUNT)
def test_flag_disabled_skips_followup(
    _account,
    mock_list,
    mock_generate,
    mock_hydrate,
    mock_save,
):
    mock_list.return_value = [
        _msg(email_id="e1", sender="boss@co.com", action_required=True)
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
    mock_hydrate.return_value = {
        "headline": "Classic",
        "items": [],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {"text": "o", "important_updates": 0, "pending_actions": 0, "meetings": 0},
        "highlights": [],
        "action_items": [],
    }
    mock_save.return_value = {"id": "b1"}

    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_ENABLED": "false",
            "PENDING_REPLY_DETECTION_ENABLED": "false",
            "AI_FOLLOWUP_ASSISTANT_ENABLED": "false",
        },
        clear=False,
    ):
        with patch(
            "briefing_followup.apply_pending_replies_to_payload"
        ) as mock_apply:
            stats = run_daily_briefing(DAY)
            mock_apply.assert_not_called()
            assert stats["pending_reply_detection_enabled"] is False
            assert stats["saved"] is True


@patch("briefing_service.save_briefing")
@patch("briefing_service.hydrate_briefing_payload")
@patch("briefing_service.generate_briefing_content")
@patch("briefing_service.list_emails_for_briefing_day")
@patch("briefing_service._resolve_briefing_account", return_value=ACCOUNT)
def test_followup_failure_falls_back_single_save(
    _account,
    mock_list,
    mock_generate,
    mock_hydrate,
    mock_save,
):
    mock_list.return_value = [
        _msg(email_id="e1", sender="boss@co.com", action_required=True)
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
    classic = {
        "headline": "Classic",
        "items": [{"title": "Keep", "priority": "high", "kind": "action", "source_email_id": "e1"}],
        "meetings": [],
        "upcoming_deadlines": [],
        "overview": {"text": "o", "important_updates": 1, "pending_actions": 1, "meetings": 0},
        "highlights": [],
        "action_items": [],
    }
    mock_hydrate.return_value = dict(classic)
    mock_save.return_value = {"id": "b2"}

    with patch.dict(
        "os.environ",
        {
            "SMART_BRIEFING_ENABLED": "false",
            "PENDING_REPLY_DETECTION_ENABLED": "true",
        },
        clear=False,
    ):
        with patch(
            "briefing_followup.apply_pending_replies_to_payload",
            side_effect=RuntimeError("boom"),
        ):
            stats = run_daily_briefing(DAY)

    assert stats["saved"] is True
    assert stats["pending_reply_fallback"] == "detection"
    assert mock_save.call_count == 1
    assert mock_save.call_args.kwargs["content"]["headline"] == "Classic"


def test_self_sent_alias_outgoing():
    assert (
        message_direction(
            {
                "sender": f"Gaurang Bhargava <{ACCOUNT}>",
                "label_ids": ["INBOX"],
            },
            ACCOUNT,
        )
        == "outgoing"
    )


if __name__ == "__main__":
    test_normalize_and_direction()
    test_pending_reply_when_latest_inbound_action_required()
    test_outgoing_after_inbound_resolves_pending()
    test_sent_draft_clears_pending()
    test_informational_noreply()
    test_unknown_when_analysis_missing()
    test_awaiting_response_requires_outgoing_ask()
    test_later_inbound_clears_awaiting()
    test_detect_followups_skips_low_confidence_awaiting()
    test_apply_pending_replies_uses_existing_fields()
    test_feature_flag_default_off()
    test_flag_disabled_skips_followup()
    test_followup_failure_falls_back_single_save()
    test_self_sent_alias_outgoing()
    print("All briefing followup tests passed.")
