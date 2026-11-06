"""Regression tests for multilingual, ranked, context-aware inbox chat."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from briefing_service import date_iso_to_internal_ms_range
from chat_service import (
    detect_response_language,
    format_count_answer,
    parse_date_windows,
    parse_sender,
    retrieve_emails_for_question,
    run_chat,
    ChatRetrieval,
)
from schemas import InboxQueryPlan

KOLKATA = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 11, 5)
ACCOUNT = "me@example.com"


def _mail(
    eid,
    *,
    subject,
    sender="Client <c@example.com>",
    importance="high",
    action=False,
    body="",
    labels=None,
    category="work",
    summary="",
    deadlines=None,
    internal_date=1_762_300_000_000,
    thread=None,
    gmail_id=None,
):
    return {
        "id": eid,
        "gmail_message_id": gmail_id or f"gmail-{eid}",
        "gmail_thread_id": thread or f"thread-{eid}",
        "subject": subject,
        "sender": sender,
        "body": body,
        "snippet": body[:120],
        "label_ids": labels or ["INBOX"],
        "internal_date": internal_date,
        "received_at": "2026-11-05T10:00:00+05:30",
        "email_analyses": [
            {
                "summary": summary or body[:180],
                "importance": importance,
                "action_required": action,
                "category": category,
                "deadlines": deadlines or [],
                "confidence": 0.9,
            }
        ],
    }


def _retrieve(question, emails, **kwargs):
    search = MagicMock(return_value=list(emails))
    counter = MagicMock(return_value=len(emails))
    stored = {row["id"]: row for row in emails}

    def get_stored(eid):
        return stored.get(eid)

    with patch("chat_service.plan_inbox_query") as plan:
        plan.return_value = InboxQueryPlan(question_kind="focused")
        result = retrieve_emails_for_question(
            question,
            gmail_account_email=kwargs.get("account", ACCOUNT),
            get_stored_email=get_stored,
            search_emails_for_chat=search,
            list_stored_emails=MagicMock(),
            count_emails_for_chat=counter,
            today=kwargs.get("today", TODAY),
            history=kwargs.get("history"),
            email_id=kwargs.get("email_id"),
            sync_state=kwargs.get("sync_state"),
        )
    return result, search, counter, plan


def test_top_five_today_is_ranked_not_a_fake_sender():
    question = "list top 5 important mails from the ones i received today"
    assert parse_sender(question) is None
    rows = [
        _mail(f"e{i}", subject=f"Matter {i}", importance="high" if i < 3 else "low", internal_date=1000 + i)
        for i in range(1, 8)
    ]
    rows.append(
        _mail(
            "promo",
            subject="Creator Bytes: Partner chats",
            body="Unsubscribe. View this email in your browser. Partner chats and customer stories.",
            labels=["INBOX", "CATEGORY_PROMOTIONS"],
            category="other",
            importance="high",
            internal_date=5000,
        )
    )
    result, search, _counter, plan = _retrieve(question, rows)
    assert plan.call_count == 0
    assert result.question_kind == "focused"
    assert result.sender_contains is None
    assert result.date_from_iso == "2026-11-05"
    assert result.date_to_iso == "2026-11-05"
    assert result.direct_list is True
    assert len(result.emails) == 5
    assert "promo" not in {row["id"] for row in result.emails}
    kwargs = search.call_args.kwargs
    assert kwargs["gmail_account_email"] == ACCOUNT
    assert kwargs["sender_contains"] is None
    assert kwargs["in_inbox_only"] is True
    assert kwargs["importance"] is None
    assert kwargs["action_required"] is None
    start_ms, end_ms = date_iso_to_internal_ms_range("2026-11-05", "2026-11-05", tz=KOLKATA)
    assert kwargs["internal_date_from_ms"] == start_ms
    assert kwargs["internal_date_to_ms"] == end_ms
    assert end_ms - start_ms == 24 * 60 * 60 * 1000
    reasons = [result.rank_notes[row["id"]]["reason"] for row in result.emails]
    assert all(reasons)
    answer = run_chat(question=question, retrieval=result)["answer"]
    assert "from ones" not in answer.lower()
    assert "0 emails" not in answer.lower()


def test_yesterday_received_versus_sent_and_all_mail():
    yesterday, search, _, _ = _retrieve(
        "Show all emails I received yesterday",
        [_mail("y1", subject="Yesterday note")],
    )
    assert yesterday.date_from_iso == "2026-11-04"
    assert yesterday.mailbox == "inbox"
    assert search.call_args.kwargs["in_inbox_only"] is True
    assert search.call_args.kwargs["label_contains"] is None

    sent, search, _, _ = _retrieve(
        "Which emails did I send today?",
        [_mail("s1", subject="Sent note", labels=["SENT"])],
    )
    assert sent.mailbox == "sent"
    assert search.call_args.kwargs["in_inbox_only"] is False
    assert search.call_args.kwargs["label_contains"] == "SENT"
    assert len(sent.emails) == 1

    everything, search, _, _ = _retrieve(
        "Show all mail from today",
        [_mail("a1", subject="Anywhere")],
    )
    assert everything.mailbox == "all"
    assert search.call_args.kwargs["in_inbox_only"] is False
    assert search.call_args.kwargs["label_contains"] is None


def test_account_isolation_and_empty_coverage():
    _result, search, _, _ = _retrieve(
        "list top 5 important emails today",
        [],
        account="other@example.com",
    )
    assert search.call_args.kwargs["gmail_account_email"] == "other@example.com"
    result, _, _, _ = _retrieve(
        "list top 5 important emails today",
        [],
        sync_state={
            "backfill_status": "in_progress",
            "updated_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        },
    )
    answer = run_chat(question="list top 5 important emails today", retrieval=result)["answer"]
    assert "ones" not in answer.lower()
    assert "backfill" in answer.lower()
    assert "not proof" in answer.lower()
    assert "0 emails in Inbox from" not in answer


def test_kolkata_day_is_half_open():
    start_ms, end_ms = date_iso_to_internal_ms_range("2026-11-05", "2026-11-05", tz=KOLKATA)
    start = datetime.fromtimestamp(start_ms / 1000, tz=KOLKATA)
    end = datetime.fromtimestamp(end_ms / 1000, tz=KOLKATA)
    assert start == datetime(2026, 11, 5, 0, 0, tzinfo=KOLKATA)
    assert end == datetime(2026, 11, 6, 0, 0, tzinfo=KOLKATA)
    assert end_ms > start_ms


def test_reply_classification_keeps_requests_and_drops_promos():
    client = _mail(
        "client",
        subject="Please review the GST working",
        body="Could you review the computation and send the signed copy?",
        action=True,
        importance="medium",
    )
    newsletter = _mail(
        "news",
        subject="Creator Bytes: Partner chats, customer stories, and more!",
        body="Unsubscribe. View in your browser. Partner chats and customer stories.",
        labels=["INBOX", "CATEGORY_PROMOTIONS"],
        category="other",
        action=True,
        importance="high",
    )
    webinar = _mail(
        "web",
        subject="Register now for the webinar",
        body="Register now for our webinar. View this email in your browser. You're receiving this update.",
        action=True,
        importance="high",
    )
    update = _mail(
        "law",
        subject="Reassessment notice valid as AO had bona fide belief",
        body="INCOME TAX case law update. The notice was held valid. No reply is requested.",
        action=False,
        importance="high",
        category="work",
    )
    result, _, _, _ = _retrieve(
        "What are my pending replies?",
        [client, newsletter, webinar, update],
    )
    ids = [row["id"] for row in result.emails]
    assert ids == ["client"]
    assert result.rank_notes["client"]["reason"]

    outgoing = _mail(
        "out",
        subject="Signed agreement",
        body="Could you send the signed agreement by Friday?",
        labels=["SENT"],
        thread="t-out",
        internal_date=100,
    )
    reply = _mail(
        "in",
        subject="Re: Signed agreement",
        body="Attached.",
        sender="Other <o@example.com>",
        labels=["INBOX"],
        thread="t-out",
        internal_date=200,
    )
    waiting, _, _, _ = _retrieve(
        "Which outgoing requests are still unanswered?",
        [outgoing],
    )
    assert [row["id"] for row in waiting.emails] == ["out"]
    answered, _, _, _ = _retrieve(
        "Which outgoing requests are still unanswered?",
        [outgoing, reply],
    )
    assert answered.emails == []


def test_deadlines_newsletters_and_duplicate_threads():
    due = _mail(
        "due",
        subject="GST payment",
        deadlines=["20 Nov 2026"],
        summary="GST payment is due on 20 Nov 2026.",
    )
    plain = _mail("plain", subject="Hello", deadlines=[])
    found, _, _, _ = _retrieve("What deadlines do I have this week?", [plain, due])
    assert [row["id"] for row in found.emails] == ["due"]
    assert "20 Nov 2026" in found.rank_notes["due"]["reason"]

    news = _mail(
        "n1",
        subject="Weekly brief",
        labels=["INBOX", "CATEGORY_PROMOTIONS"],
        category="newsletter",
    )
    listed, _, _, _ = _retrieve("Show newsletters received this week", [plain, news])
    assert [row["id"] for row in listed.emails] == ["n1"]

    older = _mail("old", subject="Thread", thread="same", internal_date=1, importance="low")
    newer = _mail("new", subject="Thread", thread="same", internal_date=9, importance="high")
    ranked, _, _, _ = _retrieve(
        "list top 5 important emails today",
        [older, newer],
    )
    assert [row["id"] for row in ranked.emails] == ["new"]


def test_hindi_hinglish_dates_and_language_switch():
    assert parse_date_windows("Aaj ke top 5 important emails batao", today=TODAY)[0].date_from == TODAY
    kal = parse_date_windows("Kal Rahul ka koi mail aaya tha?", today=TODAY)[0]
    assert kal.date_from == date(2026, 11, 4)
    assert parse_sender("Kal Rahul ka koi mail aaya tha?") == "Rahul"
    span = parse_date_windows("pichle teen din ke emails", today=TODAY)[0]
    assert span.date_from == date(2026, 11, 3)
    assert span.date_to == TODAY
    assert detect_response_language("Aaj ke top 5 important emails batao.") == "hinglish"
    assert detect_response_language("Mujhe kin emails ka reply dena hai?") == "hinglish"
    assert detect_response_language("Is email ka short summary Hindi mein do.") == "hi"
    assert detect_response_language("Show today's urgent emails in English.") == "en"
    result, search, _, plan = _retrieve("Aaj ke top 5 important emails batao.", [_mail("h1", subject="Note")])
    assert plan.call_count == 0
    assert result.response_language == "hinglish"
    assert result.date_from_iso == "2026-11-05"
    assert search.call_args.kwargs["sender_contains"] is None
    answer = run_chat(question="Aaj ke top 5 important emails batao.", retrieval=result)["answer"]
    assert "Note" in answer


def test_conversation_ordinals_and_draft_does_not_send():
    sources = [
        {"email_id": "e1", "subject": "First"},
        {"email_id": "e2", "subject": "Second"},
        {"email_id": "e3", "subject": "Third"},
    ]
    emails = [
        _mail("e1", subject="First"),
        _mail("e2", subject="Second", sender="Second Person <s@example.com>"),
        _mail("e3", subject="Third"),
    ]
    history = [
        {"role": "user", "content": "Show my top five emails today."},
        {"role": "assistant", "content": "5 emails", "sources": sources},
    ]
    second, search, _, _ = _retrieve("Summarize the second one.", emails, history=history)
    assert search.call_count == 0
    assert [row["id"] for row in second.emails] == ["e2"]

    who, _, _, _ = _retrieve(
        "Who sent it?",
        emails,
        history=history
        + [
            {"role": "user", "content": "Summarize the second one."},
            {
                "role": "assistant",
                "content": "Second.",
                "sources": [{"email_id": "e2", "subject": "Second"}],
            },
        ],
    )
    assert [row["id"] for row in who.emails] == ["e2"]

    ambiguous, _, _, _ = _retrieve("Who sent it?", emails, history=history)
    assert ambiguous.needs_clarification
    assert "second" in ambiguous.needs_clarification.lower()

    third, _, _, _ = _retrieve(
        "Summarize the third one.",
        emails,
        history=history
        + [
            {"role": "assistant", "content": "Only the second.", "sources": [{"email_id": "e2"}]}
        ],
    )
    assert [row["id"] for row in third.emails] == ["e3"]

    missing, _, _, _ = _retrieve("Who sent it?", emails, history=[])
    assert missing.needs_clarification

    draft_rows, _, _, _ = _retrieve(
        "Draft a reply.",
        emails,
        history=[
            {
                "role": "assistant",
                "content": "Second.",
                "sources": [{"email_id": "e2", "subject": "Second"}],
            }
        ],
    )
    assert draft_rows.question_kind == "draft_reply"
    assert draft_rows.emails[0]["id"] == "e2"
    send = MagicMock(side_effect=AssertionError("must not send"))
    saved = run_chat(
        question="Draft a reply.",
        retrieval=draft_rows,
        generate_email_draft=lambda email: "Thanks, I will review it.",
        save_draft=lambda email_id, body: {
            "id": "d1",
            "email_id": email_id,
            "body": body,
            "status": "pending",
        },
    )
    assert saved["draft"]["status"] == "pending"
    assert "sent the email" not in saved["answer"].lower()
    send.assert_not_called()


def test_attachments_are_a_limitation_and_search_failure_is_reported():
    limited, search, _, _ = _retrieve("Show emails with attachments.", [_mail("a", subject="File")])
    assert search.call_count == 0
    assert "attachment" in (limited.needs_clarification or "").lower()

    search = MagicMock(side_effect=RuntimeError("db down"))
    with patch("chat_service.plan_inbox_query", return_value=InboxQueryPlan()):
        failed = retrieve_emails_for_question(
            "list top 5 important emails today",
            gmail_account_email=ACCOUNT,
            get_stored_email=MagicMock(),
            search_emails_for_chat=search,
            list_stored_emails=MagicMock(),
            count_emails_for_chat=MagicMock(),
            today=TODAY,
        )
    assert "couldn't read" in (failed.needs_clarification or "").lower()


def test_malformed_plan_does_not_crash_and_zero_count_warns():
    with patch("chat_service.plan_inbox_query", side_effect=RuntimeError("bad json")):
        result = retrieve_emails_for_question(
            "What is the capital of France?",
            gmail_account_email=ACCOUNT,
            get_stored_email=MagicMock(),
            search_emails_for_chat=MagicMock(),
            list_stored_emails=MagicMock(),
            count_emails_for_chat=MagicMock(),
            today=TODAY,
        )
    assert result.question_kind == "general"
    payload = ChatRetrieval(
        question_kind="count",
        match_count=0,
        date_from_iso="2026-11-05",
        date_to_iso="2026-11-05",
        sender_contains=None,
        freshness_note="The last sync checkpoint is 90 minutes old.",
    )
    text = format_count_answer(payload)
    assert "from ones" not in text
    assert "not proof" in text


def test_long_history_does_not_reject_the_next_question():
    from schemas import ChatRequest

    request = ChatRequest(
        question="Which emails require my reply right now?",
        history=[
            {"role": "user", "content": "What important emails need my attention?"},
            {"role": "assistant", "content": "y" * 5000, "sources": []},
        ],
    )
    assert request.history is not None
    assert len(request.history[1].content) == 4000
    assert request.history[1].content.endswith("...")
    result, search, _, plan = _retrieve(
        "Which emails require my reply right now?",
        [
            _mail(
                "client",
                subject="Please review the GST working",
                body="Could you review the computation and send the signed copy?",
                action=True,
                importance="medium",
            )
        ],
    )
    assert plan.call_count == 0
    assert [row["id"] for row in result.emails] == ["client"]
    assert search.call_args.kwargs["gmail_account_email"] == ACCOUNT


def test_hindi_last_three_days_is_not_october_2023():
    question = "पिछले तीन दिनों में मुझे कौन से \u095bरूरी ईमेल मिले हैं?"
    windows = parse_date_windows(question, today=TODAY)
    assert windows[0].date_from == date(2026, 11, 3)
    assert windows[0].date_to == TODAY
    rows = [
        _mail("recent", subject="Audit file", importance="high", internal_date=3),
        _mail("older", subject="Old note", importance="low", internal_date=1),
        _mail(
            "promo",
            subject="Daily newsletter",
            labels=["INBOX", "CATEGORY_PROMOTIONS"],
            category="newsletter",
            importance="high",
            internal_date=9,
        ),
    ]
    result, search, _, plan = _retrieve(question, rows)
    assert plan.call_count == 0
    assert result.date_from_iso == "2026-11-03"
    assert result.date_to_iso == "2026-11-05"
    assert result.response_language == "hi"
    assert result.sender_contains is None
    assert result.direct_list is True
    assert result.emails[0]["id"] == "recent"
    assert "promo" not in {row["id"] for row in result.emails}
    assert len(result.emails) <= 5
    answer = run_chat(question=question, retrieval=result)["answer"]
    assert "2023" not in answer
    assert "Oct" not in answer
    assert "Audit file" in answer
    start_ms, end_ms = date_iso_to_internal_ms_range("2026-11-03", "2026-11-05", tz=KOLKATA)
    assert search.call_args.kwargs["internal_date_from_ms"] == start_ms
    assert search.call_args.kwargs["internal_date_to_ms"] == end_ms
    poisoned = InboxQueryPlan(
        date_from_iso="2023-10-20",
        date_to_iso="2023-10-23",
        question_kind="focused",
    )
    from chat_service import refine_plan

    cleaned = refine_plan(question, poisoned, today=TODAY)
    assert cleaned.date_from_iso == "2026-11-03"
    assert cleaned.date_to_iso == "2026-11-05"
    assert cleaned.rank_by == "importance"


def test_pagination_keeps_requested_count():
    rows = [
        _mail(f"p{i}", subject=f"Item {i}", importance="high" if i > 3 else "low", internal_date=i)
        for i in range(1, 9)
    ]
    result, search, _, _ = _retrieve("list top 5 important emails today", rows)
    assert len(result.emails) == 5
    assert result.match_count == 8
    assert result.truncated is True
    assert search.call_args.kwargs["limit"] >= 5


if __name__ == "__main__":
    test_top_five_today_is_ranked_not_a_fake_sender()
    test_yesterday_received_versus_sent_and_all_mail()
    test_account_isolation_and_empty_coverage()
    test_kolkata_day_is_half_open()
    test_reply_classification_keeps_requests_and_drops_promos()
    test_deadlines_newsletters_and_duplicate_threads()
    test_hindi_hinglish_dates_and_language_switch()
    test_conversation_ordinals_and_draft_does_not_send()
    test_attachments_are_a_limitation_and_search_failure_is_reported()
    test_malformed_plan_does_not_crash_and_zero_count_warns()
    test_pagination_keeps_requested_count()
    test_long_history_does_not_reject_the_next_question()
    test_hindi_last_three_days_is_not_october_2023()
    print("All chat assistant tests passed.")
