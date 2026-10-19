"""Unit tests for Ask My Inbox retrieval planning (mocked OpenAI)."""

from unittest.mock import MagicMock, patch

from chat_service import (
    emails_to_sources,
    retrieve_emails_for_question,
    run_chat,
)
from schemas import InboxQueryPlan


def test_emails_to_sources_maps_ids():
    sources = emails_to_sources(
        [
            {
                "id": "e1",
                "subject": "Hello",
                "sender": "a@b.com",
                "gmail_thread_id": "t1",
            }
        ]
    )
    assert sources == [
        {
            "email_id": "e1",
            "subject": "Hello",
            "sender": "a@b.com",
            "gmail_thread_id": "t1",
        }
    ]


def test_retrieve_uses_selected_email_id():
    get_stored = MagicMock(
        return_value={"id": "e1", "subject": "Only this"}
    )
    search = MagicMock()
    listed = MagicMock()

    rows = retrieve_emails_for_question(
        "anything",
        gmail_account_email="me@example.com",
        email_id="e1",
        get_stored_email=get_stored,
        search_emails_for_chat=search,
        list_stored_emails=listed,
    )
    assert rows == [{"id": "e1", "subject": "Only this"}]
    search.assert_not_called()
    listed.assert_not_called()


@patch("chat_service.plan_inbox_query")
def test_retrieve_recent_fallback(mock_plan):
    mock_plan.return_value = InboxQueryPlan(use_recent_fallback=True)
    listed = MagicMock(return_value=[{"id": "r1"}])
    search = MagicMock()

    rows = retrieve_emails_for_question(
        "what's new?",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=listed,
    )
    assert rows == [{"id": "r1"}]
    listed.assert_called_once()
    search.assert_not_called()


@patch("chat_service.plan_inbox_query")
def test_retrieve_applies_filters(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="alice",
        subject_keywords=["invoice"],
        importance="high",
        action_required=True,
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": "f1"}])
    listed = MagicMock()

    rows = retrieve_emails_for_question(
        "high importance invoices from alice needing reply",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=listed,
    )
    assert rows == [{"id": "f1"}]
    kwargs = search.call_args.kwargs
    assert kwargs["sender_contains"] == "alice"
    assert "invoice" in kwargs["keywords"]
    assert kwargs["importance"] == "high"
    assert kwargs["action_required"] is True
    listed.assert_not_called()


@patch("chat_service.answer_email_question", return_value="Because reasons.")
def test_run_chat_returns_sources(mock_answer):
    emails = [
        {
            "id": "e9",
            "subject": "Sub",
            "sender": "s@x.com",
            "gmail_thread_id": "th",
        }
    ]
    result = run_chat(question="why?", emails=emails, history=None)
    assert result["answer"] == "Because reasons."
    assert result["sources"][0]["email_id"] == "e9"
    mock_answer.assert_called_once()
