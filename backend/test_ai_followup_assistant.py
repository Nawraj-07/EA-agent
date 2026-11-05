"""Tests for AI Follow-up Assistant (additive; default flag off)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from ai_followup_assistant import (
    ai_followup_assistant_enabled,
    count_verified_followup_attempts,
    is_automatic_reply,
    looks_like_informational_outgoing,
    looks_like_outgoing_request,
    relevant_incoming_response,
    stable_followup_key,
    threshold_elapsed,
    threshold_hours_for_priority,
    waiting_duration_hours,
)
from briefing_followup import (
    classify_thread_state,
    detect_followups_for_emails,
    followup_detection_active,
    message_direction,
)
from briefing_recommendations import recommendation_actions_enabled


ACCOUNT = "gaurang@rbhargavaassociates.in"


def _msg(
    *,
    email_id: str,
    sender: str,
    subject: str = "Hello",
    thread: str = "t1",
    internal_date: int = 1_700_000_000_000,
    labels: list[str] | None = None,
    action_required: bool | None = None,
    importance: str = "medium",
    summary: str = "Summary",
    body: str | None = None,
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
        "body": body if body is not None else summary,
        "internal_date": internal_date,
        "label_ids": labels or ["INBOX"],
        "email_analyses": [analysis],
    }


def test_flag_default_off():
    with patch.dict(
        "os.environ",
        {
            "AI_FOLLOWUP_ASSISTANT_ENABLED": "false",
            "PENDING_REPLY_DETECTION_ENABLED": "false",
        },
        clear=False,
    ):
        assert ai_followup_assistant_enabled() is False
        assert followup_detection_active() is False
    with patch.dict(
        "os.environ",
        {
            "AI_FOLLOWUP_ASSISTANT_ENABLED": "true",
            "PENDING_REPLY_DETECTION_ENABLED": "false",
        },
        clear=False,
    ):
        assert ai_followup_assistant_enabled() is True
        assert followup_detection_active() is True


def test_thresholds_defaults_and_priority():
    assert threshold_hours_for_priority("high") == 24
    assert threshold_hours_for_priority("medium") == 48
    assert threshold_hours_for_priority("low") == 72
    assert threshold_elapsed(priority="high", waiting_hours=23) is False
    assert threshold_elapsed(priority="high", waiting_hours=24) is True


def test_informational_outgoing_not_request():
    email = _msg(
        email_id="e1",
        sender=ACCOUNT,
        summary="FYI — shipping the deck. No action needed.",
        body="FYI — shipping the deck. No action needed.",
        labels=["SENT"],
        action_required=False,
    )
    assert looks_like_informational_outgoing(email, email["email_analyses"][0]) is True
    assert looks_like_outgoing_request(email, email["email_analyses"][0]) is False


def test_outgoing_request_with_no_response_after_threshold():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old_ms = now_ms - int(60 * 3600 * 1000)  # 60h ago > medium 48h
    emails = [
        _msg(
            email_id="out1",
            sender=ACCOUNT,
            subject="Can you confirm the numbers?",
            internal_date=old_ms,
            labels=["SENT"],
            summary="Could you please confirm the Q3 numbers?",
            body="Hi — could you please confirm the Q3 numbers?",
            importance="medium",
        )
    ]
    with patch.dict(
        "os.environ",
        {
            "AI_FOLLOWUP_ASSISTANT_ENABLED": "true",
            "PENDING_REPLY_DETECTION_ENABLED": "false",
        },
        clear=False,
    ):
        with patch("briefing_followup._sent_draft_thread_ids", return_value=set()):
            found = detect_followups_for_emails(
                emails,
                account_email=ACCOUNT,
                gmail_account_email=ACCOUNT,
                list_thread_messages=lambda **_kwargs: emails,
            )
    assert len(found) >= 1
    assert found[0]["followup_state"] == "awaiting_response"
    assert found[0]["kind"] == "followup"
    assert found[0].get("recommendation_key")
    assert found[0].get("waiting_hours", 0) >= 48


def test_outgoing_request_before_threshold_not_definitive():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    recent_ms = now_ms - int(2 * 3600 * 1000)  # 2h ago
    emails = [
        _msg(
            email_id="out2",
            sender=ACCOUNT,
            subject="Quick confirm?",
            internal_date=recent_ms,
            labels=["SENT"],
            summary="Could you confirm?",
            body="Could you confirm by EOD?",
        )
    ]
    with patch.dict(
        "os.environ",
        {"AI_FOLLOWUP_ASSISTANT_ENABLED": "true"},
        clear=False,
    ):
        with patch("briefing_followup._sent_draft_thread_ids", return_value=set()):
            found = detect_followups_for_emails(
                emails,
                account_email=ACCOUNT,
                gmail_account_email=ACCOUNT,
                list_thread_messages=lambda **_kwargs: emails,
            )
    assert found == []


def test_human_reply_resolves_auto_reply_does_not():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out_ms = now_ms - int(72 * 3600 * 1000)
    auto_ms = now_ms - int(70 * 3600 * 1000)
    human_ms = now_ms - int(1 * 3600 * 1000)

    outgoing = _msg(
        email_id="out3",
        sender=ACCOUNT,
        subject="Need approval",
        internal_date=out_ms,
        labels=["SENT"],
        summary="Please approve the proposal?",
        body="Please approve the proposal when you can.",
    )
    auto = _msg(
        email_id="auto1",
        sender="boss@co.com",
        subject="Out of Office",
        internal_date=auto_ms,
        summary="I am out of office until Monday.",
        body="Automatic reply: I am out of office until Monday.",
    )
    assert is_automatic_reply(auto) is True

    with_auto = [outgoing, auto]
    classified = classify_thread_state(with_auto, account_email=ACCOUNT)
    assert classified["state"] == "awaiting_response"

    human = _msg(
        email_id="in1",
        sender="boss@co.com",
        subject="Re: Need approval",
        internal_date=human_ms,
        summary="Approved — go ahead.",
        body="Approved — go ahead.",
        action_required=False,
        importance="low",
    )
    reply = relevant_incoming_response(
        [outgoing, auto, human],
        account_email=ACCOUNT,
        after_ms=out_ms,
        message_direction_fn=message_direction,
    )
    assert reply is not None
    assert reply["id"] == "in1"


def test_attempt_count_ignores_drafts_counts_sent():
    base = int(datetime.now(timezone.utc).timestamp() * 1000) - 100_000_000
    messages = [
        _msg(
            email_id="req",
            sender=ACCOUNT,
            subject="Confirm?",
            internal_date=base,
            labels=["SENT"],
            summary="Could you confirm?",
        ),
        _msg(
            email_id="draftish",
            sender=ACCOUNT,
            subject="Re: Confirm?",
            internal_date=base + 1000,
            labels=["DRAFT"],
            summary="Just checking in",
        ),
        _msg(
            email_id="sent2",
            sender=ACCOUNT,
            subject="Re: Confirm?",
            internal_date=base + 2000,
            labels=["SENT"],
            summary="Just checking in — any update?",
        ),
    ]
    count = count_verified_followup_attempts(
        messages,
        account_email=ACCOUNT,
        request_message_id="req",
        message_direction_fn=message_direction,
    )
    assert count == 1


def test_stable_key_account_isolation():
    a = stable_followup_key(
        account_email="a@ex.com",
        gmail_thread_id="t1",
        request_message_id="m1",
    )
    b = stable_followup_key(
        account_email="b@ex.com",
        gmail_thread_id="t1",
        request_message_id="m1",
    )
    assert a != b


def test_recommendation_actions_gate():
    with patch.dict(
        "os.environ",
        {
            "INTERACTIVE_BRIEFING_ENABLED": "false",
            "AI_FOLLOWUP_ASSISTANT_ENABLED": "false",
        },
        clear=False,
    ):
        assert recommendation_actions_enabled() is False
    with patch.dict(
        "os.environ",
        {
            "INTERACTIVE_BRIEFING_ENABLED": "false",
            "AI_FOLLOWUP_ASSISTANT_ENABLED": "true",
        },
        clear=False,
    ):
        assert recommendation_actions_enabled() is True


def test_waiting_duration_positive():
    past = datetime.now(timezone.utc) - timedelta(hours=30)
    email = {
        "internal_date": int(past.timestamp() * 1000),
    }
    hours = waiting_duration_hours(email)
    assert 29 <= hours <= 31


def test_sync_refresh_noop_when_disabled():
    from ai_followup_assistant import refresh_followups_after_sync

    with patch.dict(
        "os.environ", {"AI_FOLLOWUP_ASSISTANT_ENABLED": "false"}, clear=False
    ):
        result = refresh_followups_after_sync(ACCOUNT)
    assert result["enabled"] is False
    assert result["processed"] == 0


def test_duplicate_prevention_same_key():
    key1 = stable_followup_key(
        account_email=ACCOUNT, gmail_thread_id="t9", request_message_id="m9"
    )
    key2 = stable_followup_key(
        account_email=ACCOUNT, gmail_thread_id="t9", request_message_id="m9"
    )
    assert key1 == key2


def test_human_reply_emits_resolved_keys():
    from briefing_followup import detect_followups_for_account_sync

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out_ms = now_ms - int(60 * 3600 * 1000)
    reply_ms = now_ms - int(1 * 3600 * 1000)
    outgoing = _msg(
        email_id="out-r",
        sender=ACCOUNT,
        subject="Can you confirm?",
        internal_date=out_ms,
        labels=["SENT"],
        summary="Could you confirm the numbers?",
        body="Could you confirm the numbers?",
    )
    human = _msg(
        email_id="in-r",
        sender="boss@co.com",
        subject="Re: Can you confirm?",
        internal_date=reply_ms,
        summary="Confirmed.",
        body="Confirmed — looks good.",
        action_required=False,
        importance="low",
    )
    with patch.dict(
        "os.environ", {"AI_FOLLOWUP_ASSISTANT_ENABLED": "true"}, clear=False
    ):
        detected = detect_followups_for_account_sync(
            [outgoing, human],
            account_email=ACCOUNT,
            gmail_account_email=ACCOUNT,
            list_thread_messages=lambda **_kwargs: [outgoing, human],
        )
    assert detected.get("active") == []
    assert detected.get("resolved_keys")


if __name__ == "__main__":
    test_flag_default_off()
    test_thresholds_defaults_and_priority()
    test_informational_outgoing_not_request()
    test_outgoing_request_with_no_response_after_threshold()
    test_outgoing_request_before_threshold_not_definitive()
    test_human_reply_resolves_auto_reply_does_not()
    test_attempt_count_ignores_drafts_counts_sent()
    test_stable_key_account_isolation()
    test_recommendation_actions_gate()
    test_waiting_duration_positive()
    test_sync_refresh_noop_when_disabled()
    test_duplicate_prevention_same_key()
    print("All AI follow-up assistant tests passed.")
