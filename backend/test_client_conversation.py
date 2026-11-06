"""Client conversation summaries must not fall back to an inbox dump."""

from datetime import date
from unittest.mock import MagicMock, patch

from chat_service import retrieve_emails_for_question, run_chat
from schemas import InboxQueryPlan

TODAY = date(2026, 11, 6)
ACCOUNT = "owner@example.com"


def _mail(
    eid,
    *,
    subject,
    sender,
    thread,
    internal_date,
    labels=None,
    category="work",
    summary="",
    action=False,
):
    return {
        "id": eid,
        "gmail_message_id": f"gmail-{eid}",
        "gmail_thread_id": thread,
        "subject": subject,
        "sender": sender,
        "snippet": summary or subject,
        "label_ids": labels or ["INBOX"],
        "internal_date": internal_date,
        "received_at": "2026-11-06T10:00:00+05:30",
        "email_analyses": [
            {
                "summary": summary or subject,
                "importance": "medium",
                "action_required": action,
                "category": category,
                "deadlines": [],
                "confidence": 0.8,
            }
        ],
    }


def _retrieve(question, emails, **kwargs):
    search = MagicMock(return_value=list(emails))
    with patch("chat_service.plan_inbox_query", return_value=InboxQueryPlan(question_kind="focused")):
        result = retrieve_emails_for_question(
            question,
            gmail_account_email=kwargs.get("account", ACCOUNT),
            get_stored_email=lambda eid: None,
            search_emails_for_chat=search,
            list_stored_emails=MagicMock(),
            count_emails_for_chat=MagicMock(return_value=len(emails)),
            today=TODAY,
            history=kwargs.get("history"),
        )
    return result, search


def _mixed_inbox():
    rows = [
        _mail(
            f"news-{index}",
            subject=f"Taxmann alert {index}",
            sender="Taxmann <alert@newsletter.taxmann.com>",
            thread=f"news-{index}",
            internal_date=5_000 + index,
            category="newsletter",
            labels=["INBOX", "CATEGORY_PROMOTIONS"],
        )
        for index in range(15)
    ]
    rows.append(
        _mail(
            "human-1",
            subject="GSTR1 OCT'26",
            sender="Suresh Kumar <info@medixpert.in>",
            thread="thread-suresh",
            internal_date=9_000,
            summary="GSTR-1 figures for October were attached.",
            action=True,
        )
    )
    rows.append(
        _mail(
            "human-2",
            subject="Loan delink",
            sender="Branch Desk <desk@bank.example>",
            thread="thread-bank",
            internal_date=8_000,
            summary="The branch asked which loan accounts to delink.",
        )
    )
    return rows


def test_latest_client_conversation_is_not_an_inbox_list():
    question = "Summarize my latest conversation with a client in 3 bullet points."
    result, search = _retrieve(question, _mixed_inbox())
    answer = run_chat(question=question, retrieval=result)["answer"]
    sources = run_chat(question=question, retrieval=result)["sources"]
    assert result.direct_list is False
    assert "15 emails" not in answer.lower()
    assert "Taxmann alert" not in answer
    assert "unverified" in answer.lower()
    assert "GSTR1 OCT'26" in answer
    assert "Loan delink" in answer
    kwargs = search.call_args.kwargs
    assert kwargs["gmail_account_email"] == ACCOUNT
    assert kwargs["in_inbox_only"] is False
    assert kwargs["limit"] <= 80
    assert search.call_count == 1
    ids = [item["email_id"] for item in sources]
    assert ids == ["human-1", "human-2"]
    assert len(ids) == len(set(ids))
    assert all(item.get("subject") and item.get("sender") for item in sources)


def test_list_five_recent_emails_stays_a_list():
    rows = [
        _mail(
            f"m{index}",
            subject=f"Note {index}",
            sender=f"Person {index} <p{index}@example.com>",
            thread=f"t{index}",
            internal_date=1000 + index,
        )
        for index in range(8)
    ]
    result, _search = _retrieve("List my 5 most recent emails", rows)
    assert result.direct_list is True
    assert len(result.emails) == 5


def test_summarize_latest_from_sender_is_one_email():
    rows = [
        _mail(
            "new",
            subject="Latest filing",
            sender="Rahul Shah <rahul@example.com>",
            thread="t-new",
            internal_date=2000,
            summary="The latest filing is ready for review.",
        ),
        _mail(
            "old",
            subject="Older note",
            sender="Rahul Shah <rahul@example.com>",
            thread="t-old",
            internal_date=1000,
            summary="Older filing question.",
        ),
    ]
    result, search = _retrieve("Summarize the latest email from Rahul", rows)
    assert result.direct_list is False
    assert result.sender_contains == "Rahul"
    assert [row["id"] for row in result.emails] == ["new"]
    assert len(result.emails) == 1
    assert search.call_args.kwargs["limit"] == 1
    assert search.call_args.kwargs["sender_contains"] == "Rahul"


def test_named_client_thread_is_three_bullets():
    rows = _mixed_inbox()
    rows.append(
        _mail(
            "sent-1",
            subject="Re: GSTR1 OCT'26",
            sender="Gaurang <owner@example.com>",
            thread="thread-suresh",
            internal_date=9_100,
            summary="Asked Suresh to resend the GSTR-1 sheet.",
            labels=["SENT"],
        )
    )
    question = "Summarize my latest conversation with client Suresh in 3 bullet points."
    result, _search = _retrieve(question, rows)
    answer = run_chat(question=question, retrieval=result)
    bullets = [line for line in answer["answer"].splitlines() if line.startswith("- ")]
    assert len(bullets) == 3
    assert "GSTR-1" in answer["answer"]
    assert len(answer["sources"]) == 1
    assert answer["sources"][0]["email_id"] == "sent-1"


def test_discuss_that_person_does_not_search_without_a_name():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "true"}, clear=False):
        result, search = _retrieve("What did I discuss with that person?", _mixed_inbox())
    assert search.call_count == 0
    assert result.question_kind == "person"
    answer = run_chat(question="What did I discuss with that person?", retrieval=result)["answer"]
    assert "which person" in answer.lower()


def test_reply_check_is_not_an_inbox_dump():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "true"}, clear=False):
        result, search = _retrieve(
            "Did I already reply to their last message?",
            _mixed_inbox(),
        )
    assert search.call_count == 0
    assert result.direct_list is False
    answer = run_chat(
        question="Did I already reply to their last message?",
        retrieval=result,
    )["answer"]
    assert "15 emails" not in answer.lower()
    assert "Taxmann" not in answer


def test_hinglish_client_summary_asks_instead_of_listing_mail():
    question = "Aaj meri latest client conversation ka summary do"
    result, search = _retrieve(question, [])
    answer = run_chat(question=question, retrieval=result)["answer"]
    assert result.direct_list is False
    assert search.call_args.kwargs["limit"] <= 80
    assert "client list" in answer.lower() or "client" in answer.lower()
    assert "guess" in answer.lower() or "naam" in answer.lower()


def test_general_knowledge_does_not_query_email():
    question = "Explain the difference between CC and BCC."
    result, search = _retrieve(question, _mixed_inbox())
    assert result.question_kind == "general"
    assert search.call_count == 0
    assert result.emails == []


def test_hindi_named_client_summary_has_three_bullets():
    rows = [
        _mail(
            "h1",
            subject="GST notice reply",
            sender="Anita Jain <anita@client.example>",
            thread="thread-anita",
            internal_date=3000,
            summary="Anita asked for the notice reply by Friday.",
            action=True,
            labels=["INBOX"],
        )
    ]
    question = "Summarize my latest client conversation with Anita in Hindi"
    result, _search = _retrieve(question, rows)
    answer = run_chat(question=question, retrieval=result)["answer"]
    bullets = [line for line in answer.splitlines() if line.startswith("- ")]
    assert len(bullets) == 3
    assert "मुख्य बात" in bullets[0]
    assert "Anita asked for the notice reply by Friday." in answer


def test_newsletter_only_inbox_does_not_invent_a_client():
    rows = [
        _mail(
            "n1",
            subject="Weekly digest",
            sender="News <noreply@promo.example>",
            thread="n1",
            internal_date=10,
            category="newsletter",
        )
    ]
    result, _search = _retrieve(
        "Summarize my latest conversation with a client in 3 bullet points.",
        rows,
    )
    answer = run_chat(
        question="Summarize my latest conversation with a client in 3 bullet points.",
        retrieval=result,
    )["answer"]
    assert result.emails == []
    assert "newsletter" in answer.lower() or "automated" in answer.lower()
    assert "Weekly digest" not in answer


def test_missing_sent_mail_is_disclosed():
    result, _search = _retrieve(
        "Summarize my latest conversation with a client in 3 bullet points.",
        [
            _mail(
                "only-in",
                subject="Balance confirmation",
                sender="Mehta & Co <mehta@firm.example>",
                thread="t-mehta",
                internal_date=50,
                summary="Mehta asked for a balance confirmation.",
            )
        ],
    )
    answer = run_chat(
        question="Summarize my latest conversation with a client in 3 bullet points.",
        retrieval=result,
    )["answer"]
    assert "unverified" in answer.lower()
    assert "Sent synchronization is partial" in answer


def test_ambiguous_name_lists_candidates():
    rows = [
        _mail(
            "a",
            subject="Audit file",
            sender="Rahul Shah <rahul@one.example>",
            thread="ta",
            internal_date=20,
            summary="Audit queries.",
        ),
        _mail(
            "b",
            subject="Fee note",
            sender="Rahul Mehta <rahul@two.example>",
            thread="tb",
            internal_date=30,
            summary="Fee note.",
        ),
    ]
    result, _search = _retrieve(
        "Summarize my latest conversation with client Rahul",
        rows,
    )
    answer = run_chat(
        question="Summarize my latest conversation with client Rahul",
        retrieval=result,
    )
    assert result.prepared_answer is None
    assert "Audit file" in answer["answer"]
    assert "Fee note" in answer["answer"]
    assert len(answer["sources"]) == 2


if __name__ == "__main__":
    test_latest_client_conversation_is_not_an_inbox_list()
    test_list_five_recent_emails_stays_a_list()
    test_summarize_latest_from_sender_is_one_email()
    test_named_client_thread_is_three_bullets()
    test_discuss_that_person_does_not_search_without_a_name()
    test_reply_check_is_not_an_inbox_dump()
    test_hinglish_client_summary_asks_instead_of_listing_mail()
    test_general_knowledge_does_not_query_email()
    test_hindi_named_client_summary_has_three_bullets()
    test_newsletter_only_inbox_does_not_invent_a_client()
    test_missing_sent_mail_is_disclosed()
    test_ambiguous_name_lists_candidates()
    print("client conversation tests passed")
