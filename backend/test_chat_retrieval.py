"""Unit tests for Ask My Inbox retrieval planning (mocked OpenAI)."""

from datetime import date
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from briefing_service import date_iso_to_internal_ms_range
from chat_service import (
    ChatRetrieval,
    MAILBOX_START_DATE,
    _briefing_target_date,
    classify_question,
    build_email_context,
    emails_to_sources,
    format_briefing_for_chat,
    format_email_when,
    is_briefing_question,
    list_pick_index,
    parse_date_windows,
    parse_sender,
    retrieve_emails_for_question,
    run_chat,
)
from db import (
    completed_analysis_payload,
    row_matches_all_tokens,
    tokenize_search_keywords,
)
from schemas import InboxQueryPlan

SEP1_QUESTION = "How many mails were received in the Inbox on 1st September 2026?"
KOLKATA = ZoneInfo("Asia/Kolkata")


def _retrieve(**kwargs):
    defaults = {
        "gmail_account_email": "me@example.com",
        "get_stored_email": MagicMock(),
        "search_emails_for_chat": MagicMock(return_value=[]),
        "list_stored_emails": MagicMock(return_value=[]),
        "count_emails_for_chat": MagicMock(return_value=0),
    }
    defaults.update(kwargs)
    return retrieve_emails_for_question(defaults.pop("question"), **defaults)


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
        "Summarize this email",
        gmail_account_email="me@example.com",
        email_id="e1",
        get_stored_email=get_stored,
        search_emails_for_chat=search,
        list_stored_emails=listed,
    )
    assert rows.emails == [{"id": "e1", "subject": "Only this"}]
    assert rows.question_kind == "focused"
    search.assert_not_called()
    listed.assert_not_called()


def test_retrieve_draft_reply_uses_selected_email():
    get_stored = MagicMock(
        return_value={"id": "e1", "subject": "Meeting", "sender": "hr@x.com"}
    )
    search = MagicMock()

    rows = retrieve_emails_for_question(
        "Draft a reply to this email",
        gmail_account_email="me@example.com",
        email_id="e1",
        get_stored_email=get_stored,
        search_emails_for_chat=search,
        list_stored_emails=MagicMock(),
    )
    assert rows.question_kind == "draft_reply"
    assert len(rows.emails) == 1
    assert rows.emails[0]["id"] == "e1"
    search.assert_not_called()


@patch("chat_service.plan_inbox_query")
def test_retrieve_draft_reply_clarifies_when_multiple(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="Rahul",
        use_recent_fallback=False,
    )
    search = MagicMock(
        return_value=[
            {"id": "e1", "subject": "A"},
            {"id": "e2", "subject": "B"},
        ]
    )
    counter = MagicMock(return_value=2)

    rows = retrieve_emails_for_question(
        "Draft a reply to Rahul",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=MagicMock(),
        count_emails_for_chat=counter,
    )
    assert rows.question_kind == "draft_reply"
    assert rows.needs_clarification
    assert "several" in rows.needs_clarification.lower()
    assert len(rows.emails) == 2


def test_run_chat_draft_reply_saves_draft():
    email = {
        "id": "e1",
        "subject": "Hello",
        "sender": "a@b.com",
        "gmail_thread_id": "t1",
    }
    generate = MagicMock(return_value="Thanks for your note.")
    save = MagicMock(
        return_value={
            "id": "d1",
            "email_id": "e1",
            "body": "Thanks for your note.",
            "status": "pending",
        }
    )

    result = run_chat(
        question="Draft a reply",
        retrieval=ChatRetrieval(emails=[email], question_kind="draft_reply"),
        generate_email_draft=generate,
        save_draft=save,
    )
    assert result["draft"]["id"] == "d1"
    assert result["draft"]["body"] == "Thanks for your note."
    assert result["sources"][0]["email_id"] == "e1"
    assert "Draft reply" in result["answer"]
    assert "Thanks for your note." not in result["answer"]
    generate.assert_called_once_with(email)
    save.assert_called_once_with("e1", "Thanks for your note.")


def test_run_chat_draft_reply_no_reply_needed():
    email = {
        "id": "e1",
        "subject": "OTP",
        "sender": "noreply@x.com",
        "gmail_thread_id": "t1",
    }
    generate = MagicMock(return_value="NO_REPLY_NEEDED")
    save = MagicMock()

    result = run_chat(
        question="Generate a draft",
        retrieval=ChatRetrieval(emails=[email], question_kind="draft_reply"),
        generate_email_draft=generate,
        save_draft=save,
    )
    assert result["draft"] is None
    assert "does not need a reply" in result["answer"].lower()
    save.assert_not_called()


def test_run_chat_draft_reply_clarify_includes_sources():
    emails = [
        {"id": "e1", "subject": "A", "sender": "a", "gmail_thread_id": "t"},
        {"id": "e2", "subject": "B", "sender": "b", "gmail_thread_id": "t2"},
    ]
    result = run_chat(
        question="Draft a reply",
        retrieval=ChatRetrieval(
            emails=emails,
            question_kind="draft_reply",
            needs_clarification="I found several emails. Open the one you want.",
        ),
    )
    assert result["draft"] is None
    assert "several" in result["answer"].lower()
    assert len(result["sources"]) == 2


@patch("chat_service.plan_inbox_query")
def test_retrieve_recent_fallback(mock_plan):
    mock_plan.return_value = InboxQueryPlan(use_recent_fallback=True)
    listed = MagicMock(return_value=[{"id": "r1"}])
    search = MagicMock()
    counter = MagicMock()

    rows = retrieve_emails_for_question(
        "What is GST?",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=listed,
        count_emails_for_chat=counter,
    )
    assert rows.question_kind == "general"
    assert rows.emails == []
    listed.assert_not_called()
    search.assert_not_called()
    counter.assert_not_called()


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
    counter = MagicMock(return_value=1)

    rows = retrieve_emails_for_question(
        "high importance invoices from alice needing reply",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=listed,
        count_emails_for_chat=counter,
    )
    assert rows.emails == [{"id": "f1"}]
    kwargs = search.call_args.kwargs
    assert kwargs["sender_contains"] == "alice"
    assert "invoice" in kwargs["keywords"]
    assert kwargs["importance"] == "high"
    assert kwargs["action_required"] is True
    assert kwargs["limit"] == 15
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


@patch("chat_service.answer_email_question", return_value="The latest note from Raj.")
def test_last_email_returns_one_matching_source(mock_answer):
    emails = [
        {
            "id": "1",
            "subject": "NICAL Update",
            "sender": "CA Raj Chawla <rajchawlacal6@gmail.com>",
            "gmail_thread_id": "t1",
        },
        {
            "id": "2",
            "subject": "Webinar",
            "sender": "ICAI <events@icai.org>",
            "gmail_thread_id": "t2",
        },
        {
            "id": "3",
            "subject": "Another webinar",
            "sender": "ICAI <events@icai.org>",
            "gmail_thread_id": "t3",
        },
    ]
    result = run_chat(
        question="What was the last email from Raj Chawla?",
        retrieval=ChatRetrieval(emails=emails, question_kind="focused"),
    )
    assert len(result["sources"]) == 1
    assert result["sources"][0]["email_id"] == "1"
    mock_answer.assert_called_once()


@patch("chat_service.answer_email_question", return_value="A few notes.")
def test_focused_list_returns_sources_in_order(mock_answer):
    emails = [
        {
            "id": str(i),
            "subject": f"Note {i}",
            "sender": "Taxmann <alert@newsletter.taxmann.com>",
            "gmail_thread_id": f"t{i}",
        }
        for i in range(1, 6)
    ]
    result = run_chat(
        question="What did Taxmann send?",
        retrieval=ChatRetrieval(emails=emails, question_kind="focused"),
    )
    assert [source["email_id"] for source in result["sources"]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    mock_answer.assert_called_once()


@patch("chat_service.answer_email_question", return_value="Themes.")
def test_broad_summary_sources_capped_at_five(mock_answer):
    emails = [
        {
            "id": str(i),
            "subject": f"Note {i}",
            "sender": "News <news@example.com>",
            "gmail_thread_id": f"t{i}",
        }
        for i in range(8)
    ]
    result = run_chat(
        question="Summarize important emails from August",
        retrieval=ChatRetrieval(emails=emails, question_kind="broad_summary"),
    )
    assert len(result["sources"]) == 5
    mock_answer.assert_called_once()


def test_tokenize_keywords_splits_phrases():
    tokens = tokenize_search_keywords(["GST filing", "from Rahul"])
    assert "gst" in tokens
    assert "filing" in tokens
    assert "rahul" in tokens
    assert "from" not in tokens


def test_row_matches_all_tokens_requires_every_token():
    row = {
        "subject": "GST notice",
        "snippet": "filing reminder",
        "body": "Please file GST this week",
        "sender": "Rahul <rahul@client.com>",
        "email_analyses": [],
    }
    assert row_matches_all_tokens(row, ["gst", "filing"]) is True
    assert row_matches_all_tokens(row, ["gst", "invoice"]) is False


def test_completed_analysis_skips_openai_when_done():
    email = {
        "analysis_status": "done",
        "email_analyses": [{"summary": "Already done", "category": "work"}],
    }
    assert completed_analysis_payload(email)["summary"] == "Already done"
    pending = {"analysis_status": "pending", "email_analyses": []}
    assert completed_analysis_payload(pending) is None


@patch("chat_service.plan_inbox_query")
def test_retrieve_count_uses_postgres_count_not_search_cap(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="Rahul",
        question_kind="count",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": str(i)} for i in range(15)])
    counter = MagicMock(return_value=20)

    result = retrieve_emails_for_question(
        "How many emails came from Rahul?",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=MagicMock(),
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.match_count == 20
    assert result.emails == []
    search.assert_not_called()
    counter.assert_called_once()


@patch("chat_service.plan_inbox_query")
def test_broad_summary_marks_truncated_when_count_exceeds_retrieved(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        date_from_iso="2026-09-01",
        date_to_iso="2026-09-30",
        question_kind="broad_summary",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": str(i)} for i in range(40)])
    counter = MagicMock(return_value=80)

    result = retrieve_emails_for_question(
        "Summarize the important emails in September",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=MagicMock(),
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "broad_summary"
    assert result.truncated is True
    assert result.match_count == 80
    assert len(result.emails) == 40
    assert search.call_args.kwargs["limit"] == 40


@patch("chat_service.answer_email_question")
def test_run_chat_count_does_not_use_first_fifteen(mock_answer):
    result = run_chat(
        question="How many emails from Rahul?",
        retrieval=ChatRetrieval(
            question_kind="count",
            match_count=20,
            emails=[],
            sender_contains="Rahul",
        ),
    )
    assert "20" in result["answer"]
    assert result["sources"] == []
    mock_answer.assert_not_called()


def test_classify_count_and_comparison():
    assert classify_question(SEP1_QUESTION) == "count"
    assert classify_question("How many emails in September 2026?") == "count"
    assert classify_question("How many unread emails?") == "count"
    assert classify_question("Compare email volumes in August and September") == "comparison"
    assert classify_question("Find emails from Rahul regarding GST") is None
    assert classify_question("Summarize important emails from August") == "broad_summary"
    assert classify_question("Count Rahul's emails from October") == "count"
    assert classify_question("How many messages did I receive from Rahul between October 1 and 31?") == "count"
    world_cup = "Who won the world cup match between Ind Vs Aus in 2023"
    assert classify_question(world_cup) not in {"count", "comparison", "broad_summary"}
    assert classify_question("How many people live in India?") == "general"
    assert classify_question("What is Docker?") == "general"
    assert classify_question("Explain GST.") == "general"
    assert classify_question("Draft a professional email") == "general"
    assert classify_question("Draft a reply to this email") == "draft_reply"
    assert classify_question("Generate a draft") == "draft_reply"
    assert classify_question("Write a reply") == "draft_reply"
    assert classify_question("Write me a reply") == "draft_reply"
    assert classify_question("Prepare a response") == "draft_reply"
    assert classify_question("Summarize this email") is None
    assert classify_question("Show my daily briefing") == "briefing"
    assert classify_question("Describe me the mail from taxman today") is None
    assert classify_question("Give me a brief for Taxman email today") is None
    assert is_briefing_question("Describe me the mail from taxman today") is False
    assert is_briefing_question("Give me a brief for Taxman email today") is False
    assert classify_question("How many email did we receive from taxman today") == "count"
    assert classify_question("What's in today's briefing?") == "briefing"
    assert classify_question("What meetings are in my briefing") == "briefing"
    assert classify_question("what was the briefing of yesterday?") == "briefing"
    assert classify_question("what was yesterday's briefing?") == "briefing"
    assert classify_question("show yesterday briefing") == "briefing"
    assert classify_question("briefing yesterday") == "briefing"
    assert classify_question("tomorrow's briefing") == "briefing"


@patch("chat_service.plan_inbox_query")
def test_mail_brief_is_not_daily_briefing(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        question_kind="briefing",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": "tax-1", "subject": "Taxmann digest", "sender": "Taxmann"}])
    counter = MagicMock(return_value=1)
    result = retrieve_emails_for_question(
        "Give me a brief for Taxman email today",
        gmail_account_email="me@example.com",
        get_stored_email=MagicMock(),
        search_emails_for_chat=search,
        list_stored_emails=MagicMock(),
        count_emails_for_chat=counter,
        today=date(2026, 11, 5),
    )
    assert result.question_kind == "focused"
    assert result.briefing is None
    assert search.called
    assert (search.call_args.kwargs.get("sender_contains") or "").lower() == "taxman"


def test_briefing_relative_dates_resolve_correctly():
    today = date(2026, 11, 4)
    assert _briefing_target_date("what was the briefing of yesterday?", today) == date(
        2026, 11, 3
    )
    assert _briefing_target_date("yesterday's briefing", today) == date(2026, 11, 3)
    assert _briefing_target_date("briefing day before yesterday", today) == date(
        2026, 11, 2
    )
    assert _briefing_target_date("show briefing for the day before", today) == date(
        2026, 11, 3
    )
    assert _briefing_target_date("tomorrow's briefing", today) == date(2026, 11, 5)
    assert _briefing_target_date("day after tomorrow briefing", today) == date(
        2026, 11, 6
    )
    assert _briefing_target_date("briefing for the day after", today) == date(
        2026, 11, 5
    )
    assert _briefing_target_date("what was the briefing on 2nd november?", today) == date(
        2026, 11, 2
    )
    # day-before-yesterday must not collapse to yesterday
    wins = parse_date_windows("day before yesterday", today=today)
    assert wins[0].date_from == date(2026, 11, 2)


def test_format_and_run_chat_briefing():
    row = {
        "briefing_date": "2026-11-03",
        "email_count": 12,
        "content": {
            "headline": "Busy Monday",
            "overview": {
                "important_updates": 2,
                "pending_actions": 1,
                "meetings": 1,
                "text": "Focus on payments and the webinar.",
            },
            "items": [
                {
                    "title": "BOBCARD due",
                    "priority": "high",
                    "recommended_action": "Pay today",
                    "source_email_id": "e-bob",
                    "subject": "BOBCARD Payment Due",
                    "sender": "alerts@bob",
                }
            ],
            "meetings": [
                {
                    "event_title": "Webinar",
                    "event_date": "2026-11-03",
                    "start_time": "16:00",
                    "status": "confirmed",
                    "source_email_id": "e-web",
                }
            ],
            "upcoming_deadlines": [],
        },
    }
    text = format_briefing_for_chat(row)
    assert "Busy Monday" in text
    assert "BOBCARD due" in text
    assert "Webinar" in text

    result = run_chat(
        question="Show my daily briefing",
        retrieval=ChatRetrieval(
            emails=[],
            question_kind="briefing",
            match_count=1,
            briefing=row,
        ),
    )
    assert "Daily briefing" in result["answer"]
    assert "BOBCARD due" in result["answer"]
    assert result["draft"] is None
    assert any(src["email_id"] == "e-bob" for src in result["sources"])


def test_parse_ordinal_and_relative_dates():
    windows = parse_date_windows(SEP1_QUESTION, today=date(2026, 11, 2))
    assert len(windows) == 1
    assert windows[0].date_from == date(2026, 9, 1)
    assert windows[0].date_to == date(2026, 9, 1)

    month = parse_date_windows("How many emails in September 2026?", today=date(2026, 11, 2))
    assert month[0].date_from == date(2026, 9, 1)
    assert month[0].date_to == date(2026, 9, 30)

    ranged = parse_date_windows(
        "How many emails from 1st September to 10th September 2026?",
        today=date(2026, 11, 2),
    )
    assert len(ranged) == 1
    assert ranged[0].date_from == date(2026, 9, 1)
    assert ranged[0].date_to == date(2026, 9, 10)

    yesterday = parse_date_windows("How many emails yesterday?", today=date(2026, 11, 2))
    assert yesterday[0].date_from == date(2026, 11, 1)

    last_week = parse_date_windows("How many emails last week?", today=date(2026, 11, 2))
    assert last_week[0].date_from == date(2026, 10, 26)
    assert last_week[0].date_to == date(2026, 11, 1)

    compared = parse_date_windows(
        "Compare email volumes in August and September 2026",
        today=date(2026, 11, 2),
    )
    assert [w.date_from.month for w in compared] == [8, 9]
    assert compared[0].date_to == date(2026, 8, 31)
    assert compared[1].date_to == date(2026, 9, 30)


@patch("chat_service.plan_inbox_query")
def test_september_first_uses_sql_count_not_retrieval_limit(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="focused", use_recent_fallback=False)
    search = MagicMock(return_value=[{"id": str(i)} for i in range(15)])
    counter = MagicMock(return_value=120)
    result = _retrieve(
        question=SEP1_QUESTION,
        search_emails_for_chat=search,
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.match_count == 120
    assert result.emails == []
    search.assert_not_called()
    mock_plan.assert_not_called()
    kwargs = counter.call_args.kwargs
    assert "limit" not in kwargs
    start_ms, end_ms = date_iso_to_internal_ms_range(
        "2026-09-01",
        "2026-09-01",
        tz=KOLKATA,
    )
    assert kwargs["internal_date_from_ms"] == start_ms
    assert kwargs["internal_date_to_ms"] == end_ms
    assert kwargs["in_inbox_only"] is True
    assert kwargs["gmail_account_email"] == "me@example.com"
    assert kwargs["keywords"] == []
    answer = run_chat(question=SEP1_QUESTION, retrieval=result)
    assert "120" in answer["answer"]
    assert "15" not in answer["answer"]


@patch("chat_service.plan_inbox_query")
def test_monthly_count_september(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="focused")
    counter = MagicMock(return_value=400)
    result = _retrieve(
        question="How many emails in September 2026?",
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.date_from_iso == "2026-09-01"
    assert result.date_to_iso == "2026-09-30"
    start_ms, end_ms = date_iso_to_internal_ms_range(
        "2026-09-01",
        "2026-09-30",
        tz=KOLKATA,
    )
    assert counter.call_args.kwargs["internal_date_from_ms"] == start_ms
    assert counter.call_args.kwargs["internal_date_to_ms"] == end_ms


@patch("chat_service.plan_inbox_query")
def test_unread_count_sets_unread_only(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="focused")
    counter = MagicMock(return_value=7)
    result = _retrieve(
        question="How many unread emails?",
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.unread_only is True
    assert counter.call_args.kwargs["unread_only"] is True


@patch("chat_service.plan_inbox_query")
def test_sender_and_date_count(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="focused")
    counter = MagicMock(return_value=3)
    result = _retrieve(
        question="How many emails from Rahul on 1st September 2026?",
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.sender_contains == "Rahul"
    assert counter.call_args.kwargs["sender_contains"] == "Rahul"
    start_ms, end_ms = date_iso_to_internal_ms_range(
        "2026-09-01",
        "2026-09-01",
        tz=KOLKATA,
    )
    assert counter.call_args.kwargs["internal_date_from_ms"] == start_ms
    assert counter.call_args.kwargs["internal_date_to_ms"] == end_ms


@patch("chat_service.plan_inbox_query")
def test_date_range_count(mock_plan):
    counter = MagicMock(return_value=50)
    result = _retrieve(
        question="How many emails from 1st September to 10th September 2026?",
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.date_from_iso == "2026-09-01"
    assert result.date_to_iso == "2026-09-10"


@patch("chat_service.plan_inbox_query")
def test_comparison_uses_two_counts_not_search(mock_plan):
    search = MagicMock(return_value=[{"id": str(i)} for i in range(15)])
    counter = MagicMock(side_effect=[80, 120])
    result = _retrieve(
        question="Compare email volumes in August and September 2026",
        search_emails_for_chat=search,
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "comparison"
    assert counter.call_count == 2
    search.assert_not_called()
    for call in counter.call_args_list:
        assert "limit" not in call.kwargs
    answer = run_chat(
        question="Compare email volumes in August and September 2026",
        retrieval=result,
    )
    assert "80" in answer["answer"]
    assert "120" in answer["answer"]


@patch("chat_service.plan_inbox_query")
def test_count_zero_is_not_empty_context_sentence(mock_plan):
    result = ChatRetrieval(
        question_kind="count",
        match_count=0,
        emails=[],
        date_from_iso="2026-01-01",
        date_to_iso="2026-01-01",
    )
    answer = run_chat(question="How many emails on 1 January 2026?", retrieval=result)
    assert answer["answer"].startswith("0 ")
    assert "don't have any saved emails" not in answer["answer"].lower()


@patch("chat_service.plan_inbox_query")
def test_count_database_failure_is_not_swallowed(mock_plan):
    counter = MagicMock(side_effect=RuntimeError("db down"))
    try:
        _retrieve(
            question=SEP1_QUESTION,
            count_emails_for_chat=counter,
        )
        raise AssertionError("expected COUNT failure to propagate")
    except RuntimeError as exc:
        assert "db down" in str(exc)


@patch("chat_service.plan_inbox_query")
def test_inbox_wide_count_ignores_selected_email(mock_plan):
    get_stored = MagicMock(return_value={"id": "e1"})
    search = MagicMock()
    counter = MagicMock(return_value=120)
    result = _retrieve(
        question=SEP1_QUESTION,
        email_id="e1",
        get_stored_email=get_stored,
        search_emails_for_chat=search,
        count_emails_for_chat=counter,
    )
    assert result.question_kind == "count"
    assert result.match_count == 120
    get_stored.assert_not_called()
    search.assert_not_called()


def test_parse_sender_person_org_email_and_domain():
    assert parse_sender("How many emails from Rahul last month?") == "Rahul"
    assert parse_sender("Count Rahul's emails from October") == "Rahul"
    assert parse_sender("How many ICAI emails last month?") == "ICAI"
    assert parse_sender("How many emails from Taxmann in October?") == "Taxmann"
    assert parse_sender("How many emails from LinkedIn?") == "LinkedIn"
    assert parse_sender("Count emails from jane@client.com") == "jane@client.com"
    assert parse_sender("How many emails from @icai.org?") == "icai.org"
    assert parse_sender("How many emails from Amit Sharma yesterday?") == "Amit Sharma"
    assert parse_sender("How many emails from October?") is None


def test_equivalent_sender_month_wordings_share_filters():
    today = date(2026, 11, 2)
    questions = [
        "How many emails came from Rahul last month?",
        "Count Rahul's emails from October.",
        "How many messages did I receive from Rahul between October 1 and 31?",
    ]
    results = [
        _retrieve(question=q, count_emails_for_chat=MagicMock(return_value=198), today=today)
        for q in questions
    ]
    for result, counter_call in zip(results, [r for r in results]):
        assert result.question_kind == "count"
        assert result.sender_contains == "Rahul"
        assert result.date_from_iso == "2026-10-01"
        assert result.date_to_iso == "2026-10-31"
        assert result.keywords == []
        assert result.match_count == 198
    first_kwargs = results[0]
    # All three COUNTs used the same date/sender and never a search cap.
    for result in results:
        assert result.match_count == first_kwargs.match_count


@patch("chat_service.plan_inbox_query")
def test_count_does_not_use_sender_as_body_keyword(mock_plan):
    counter = MagicMock(return_value=198)
    result = _retrieve(
        question="How many emails from ICAI last month?",
        count_emails_for_chat=counter,
        today=date(2026, 11, 2),
    )
    assert result.question_kind == "count"
    assert result.sender_contains == "ICAI"
    assert counter.call_args.kwargs["keywords"] == []
    assert "limit" not in counter.call_args.kwargs
    assert result.date_from_iso == "2026-10-01"
    assert result.date_to_iso == "2026-10-31"


@patch("chat_service.plan_inbox_query")
def test_relative_dates_use_provided_today(mock_plan):
    counter = MagicMock(return_value=4)
    result = _retrieve(
        question="How many emails from Rahul yesterday?",
        count_emails_for_chat=counter,
        today=date(2026, 11, 2),
    )
    assert result.date_from_iso == "2026-11-01"
    assert result.date_to_iso == "2026-11-01"
    this_month = _retrieve(
        question="How many emails this month?",
        count_emails_for_chat=MagicMock(return_value=1),
        today=date(2026, 11, 2),
    )
    assert this_month.date_from_iso == "2026-11-01"
    assert this_month.date_to_iso == "2026-11-02"


@patch("chat_service.plan_inbox_query")
def test_ambiguous_sender_asks_instead_of_guessing(mock_plan):
    result = _retrieve(
        question="How many emails from them last month?",
        count_emails_for_chat=MagicMock(return_value=9),
        today=date(2026, 11, 2),
    )
    assert result.needs_clarification
    assert "sender" in result.needs_clarification.lower()
    answer = run_chat(question="How many emails from them last month?", retrieval=result)
    assert "sender" in answer["answer"].lower()


@patch("chat_service.plan_inbox_query")
def test_conflicting_dates_ask_for_clarification(mock_plan):
    result = _retrieve(
        question="How many emails last month from September?",
        count_emails_for_chat=MagicMock(return_value=9),
        today=date(2026, 11, 2),
    )
    assert result.needs_clarification
    assert "date" in result.needs_clarification.lower() or "period" in result.needs_clarification.lower()


@patch("chat_service.plan_inbox_query")
def test_partial_and_domain_sender_counts_bypass_limit(mock_plan):
    search = MagicMock(return_value=[{"id": str(i)} for i in range(15)])
    counter = MagicMock(return_value=77)
    result = _retrieve(
        question="How many emails from icai.org in October 2026?",
        search_emails_for_chat=search,
        count_emails_for_chat=counter,
        today=date(2026, 11, 2),
    )
    assert result.question_kind == "count"
    assert result.match_count == 77
    assert "icai.org" in (result.sender_contains or "").lower()
    search.assert_not_called()
    assert "limit" not in counter.call_args.kwargs


@patch("chat_service.ChatOpenAI")
def test_run_chat_general_does_not_require_emails(mock_llm_cls):
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content="GST is a tax on goods and services.")
    mock_llm_cls.return_value = mock_llm
    result = run_chat(
        question="What is GST?",
        retrieval=ChatRetrieval(question_kind="general", emails=[]),
    )
    assert "tax" in result["answer"].lower()
    assert result["sources"] == []
    mock_llm.invoke.assert_called_once()


WORLD_CUP_QUESTION = "Who won the world cup match between Ind Vs Aus in 2023"


@patch("chat_service.ChatOpenAI")
@patch("chat_service.plan_inbox_query")
def test_world_cup_question_does_not_count_mailbox(mock_plan, mock_llm_cls):
    mock_plan.return_value = InboxQueryPlan(
        question_kind="comparison",
        use_recent_fallback=False,
    )
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content="Australia won the 2023 World Cup.")
    mock_llm_cls.return_value = mock_llm
    counter = MagicMock(return_value=15330)
    search = MagicMock()
    result = _retrieve(
        question=WORLD_CUP_QUESTION,
        count_emails_for_chat=counter,
        search_emails_for_chat=search,
    )
    assert result.question_kind == "general"
    counter.assert_not_called()
    search.assert_not_called()
    answer = run_chat(question=WORLD_CUP_QUESTION, retrieval=result)
    assert "15330" not in answer["answer"]
    assert answer["sources"] == []
    mock_llm.invoke.assert_called_once()


@patch("chat_service.plan_inbox_query")
def test_email_question_without_email_keyword_searches_mailbox(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="Taxmann",
        body_keywords=["GST"],
        question_kind="focused",
        use_recent_fallback=False,
    )
    get_stored = MagicMock(return_value={"id": "selected"})
    search = MagicMock(return_value=[{"id": "t1", "sender": "Taxmann"}])
    listed = MagicMock()
    result = _retrieve(
        question="What did Taxmann send about GST in October?",
        email_id="selected",
        get_stored_email=get_stored,
        search_emails_for_chat=search,
        list_stored_emails=listed,
        today=date(2026, 11, 2),
    )
    assert result.question_kind == "focused"
    get_stored.assert_not_called()
    listed.assert_not_called()
    search.assert_called_once()
    assert search.call_args.kwargs["sender_contains"] == "Taxmann"
    assert result.date_from_iso == "2026-10-01"
    assert result.date_to_iso == "2026-10-31"
    keywords = [token.lower() for token in search.call_args.kwargs["keywords"]]
    assert "gst" in keywords


@patch("chat_service.plan_inbox_query")
def test_followup_reuses_prior_mailbox_search(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="Taxmann",
        body_keywords=["GST"],
        question_kind="focused",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": "t1"}])
    listed = MagicMock()
    history = [
        {"role": "user", "content": "What did Taxmann send about GST in October?"},
        {"role": "assistant", "content": "Taxmann sent a GST daily."},
    ]
    result = _retrieve(
        question="What did they attach?",
        search_emails_for_chat=search,
        list_stored_emails=listed,
        history=history,
        today=date(2026, 11, 2),
    )
    listed.assert_not_called()
    search.assert_called_once()
    assert result.emails == [{"id": "t1"}]
    assert search.call_args.kwargs["sender_contains"] == "Taxmann"


@patch("chat_service.plan_inbox_query")
def test_broad_summary_without_keywords_uses_full_count(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        question_kind="broad_summary",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": str(i)} for i in range(40)])
    counter = MagicMock(return_value=90)
    listed = MagicMock()
    result = _retrieve(
        question="Summarize last week",
        search_emails_for_chat=search,
        count_emails_for_chat=counter,
        list_stored_emails=listed,
        today=date(2026, 11, 2),
    )
    assert result.question_kind == "broad_summary"
    assert result.match_count == 90
    assert result.truncated is True
    listed.assert_not_called()
    assert search.call_args.kwargs["keywords"] == []
    assert result.date_from_iso == "2026-10-26"
    assert "limit" not in counter.call_args.kwargs


@patch("chat_service.plan_inbox_query")
def test_yesterday_count_uses_sql_and_kolkata_day(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="count")
    counter = MagicMock(return_value=42)
    today = date(2026, 11, 2)
    result = _retrieve(
        question="How many emails were received yesterday?",
        count_emails_for_chat=counter,
        today=today,
        email_id="selected-id",
        get_stored_email=MagicMock(return_value={"id": "selected-id"}),
    )
    assert result.question_kind == "count"
    assert result.match_count == 42
    assert result.date_from_iso == "2026-11-01"
    assert result.date_to_iso == "2026-11-01"
    kwargs = counter.call_args.kwargs
    assert kwargs["gmail_account_email"] == "me@example.com"
    assert kwargs["in_inbox_only"] is True
    assert kwargs["keywords"] == []
    start_ms, end_ms = date_iso_to_internal_ms_range(
        "2026-11-01", "2026-11-01", tz=KOLKATA
    )
    assert kwargs["internal_date_from_ms"] == start_ms
    assert kwargs["internal_date_to_ms"] == end_ms
    answer = run_chat(question="How many emails were received yesterday?", retrieval=result)
    assert answer["answer"].startswith("42 ")
    assert "don't have any saved emails" not in answer["answer"].lower()


@patch("chat_service.plan_inbox_query")
def test_undated_mailbox_count_floors_at_june_2026(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="count")
    counter = MagicMock(return_value=9)
    result = _retrieve(
        question="How many emails are in my inbox?",
        count_emails_for_chat=counter,
        today=date(2026, 11, 2),
    )
    floor_ms, _ = date_iso_to_internal_ms_range(
        MAILBOX_START_DATE.isoformat(), None, tz=KOLKATA
    )
    assert result.question_kind == "count"
    assert counter.call_args.kwargs["internal_date_from_ms"] == floor_ms
    assert counter.call_args.kwargs["gmail_account_email"] == "me@example.com"


@patch("chat_service.plan_inbox_query")
def test_followup_important_ones_reuses_prior_search(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="Taxmann",
        question_kind="focused",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": "t1"}])
    history = [
        {"role": "user", "content": "Find emails from Taxmann last month."},
        {"role": "assistant", "content": "I found several Taxmann messages."},
    ]
    result = _retrieve(
        question="Summarize the important ones.",
        search_emails_for_chat=search,
        history=history,
        today=date(2026, 11, 2),
        email_id="selected-id",
        get_stored_email=MagicMock(return_value={"id": "selected-id"}),
    )
    search.assert_called_once()
    assert result.emails == [{"id": "t1"}]
    assert search.call_args.kwargs["sender_contains"] == "Taxmann"


@patch("chat_service.plan_inbox_query")
def test_empty_keyword_search_is_not_saved_emails_sentence(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        body_keywords=["zzzxnotfound"],
        question_kind="focused",
        use_recent_fallback=False,
    )
    result = _retrieve(
        question="Find emails about zzzxnotfound",
        search_emails_for_chat=MagicMock(return_value=[]),
        count_emails_for_chat=MagicMock(return_value=0),
    )
    answer = run_chat(question="Find emails about zzzxnotfound", retrieval=result)
    assert "don't have any saved emails" not in answer["answer"].lower()
    assert "no matching" in answer["answer"].lower()


@patch("chat_service.plan_inbox_query")
def test_account_isolation_passed_to_count(mock_plan):
    mock_plan.return_value = InboxQueryPlan(question_kind="count")
    counter = MagicMock(return_value=3)
    _retrieve(
        question="How many emails yesterday?",
        gmail_account_email="gaurang@rbhargavaassociates.in",
        count_emails_for_chat=counter,
        today=date(2026, 11, 2),
    )
    assert counter.call_args.kwargs["gmail_account_email"] == (
        "gaurang@rbhargavaassociates.in"
    )


@patch("chat_service.ChatOpenAI")
def test_general_docker_question_uses_openai(mock_llm_cls):
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content="Docker packages applications.")
    mock_llm_cls.return_value = mock_llm
    result = _retrieve(question="What is Docker?")
    assert result.question_kind == "general"
    answer = run_chat(question="What is Docker?", retrieval=result)
    assert "Docker" in answer["answer"]
    mock_llm.invoke.assert_called_once()


@patch("chat_service.answer_email_question", return_value="Partial summary")
def test_run_chat_broad_summary_notes_truncated_coverage(mock_answer):
    from chat_service import ChatRetrieval

    emails = [{"id": "e1", "subject": "One", "sender": "a", "gmail_thread_id": "t"}]
    run_chat(
        question="Summarize September",
        retrieval=ChatRetrieval(
            question_kind="broad_summary",
            emails=emails,
            match_count=12,
            truncated=True,
        ),
    )
    note = mock_answer.call_args.kwargs["coverage_note"]
    assert "1 of 12" in note
    assert "every matching" in note.lower() or "all matching" in note.lower()


def _five_taxmann_sources():
    return [
        {
            "email_id": f"e{i}",
            "subject": f"Update {i}",
            "sender": "Taxmann <alert@newsletter.taxmann.com>",
        }
        for i in range(1, 6)
    ]


def test_email_context_uses_local_date():
    when = format_email_when("Thu, 05 Nov 2026 12:26:24 +0530")
    assert when == "5 Nov 2026, 12:26 PM"
    text = build_email_context(
        [
            {
                "id": "e1",
                "subject": "Update",
                "sender": "Taxmann",
                "received_at": "Thu, 05 Nov 2026 12:26:24 +0530",
            }
        ]
    )
    assert "5 Nov 2026, 12:26 PM" in text
    assert "+0530" not in text


def test_list_pick_phrases():
    assert list_pick_index("email 2") == 2
    assert list_pick_index("number 2") == 2
    assert list_pick_index("the 2nd one") == 2
    assert list_pick_index("second mail") == 2
    assert list_pick_index("emails from Taxmann today") is None


def test_email_number_returns_that_source_only():
    search = MagicMock(return_value=[{"id": "other"}])
    stored = MagicMock(
        return_value={"id": "e2", "subject": "Update 2", "sender": "Taxmann"}
    )
    history = [
        {"role": "user", "content": "What did Taxmann send today?"},
        {
            "role": "assistant",
            "content": "Five emails from Taxmann.",
            "sources": _five_taxmann_sources(),
        },
    ]
    result = _retrieve(
        question="email 2",
        history=history,
        search_emails_for_chat=search,
        get_stored_email=stored,
    )
    assert result.question_kind == "focused"
    assert [email["id"] for email in result.emails] == ["e2"]
    stored.assert_called_once_with("e2")
    search.assert_not_called()


def test_email_number_out_of_range_does_not_search():
    search = MagicMock(return_value=[{"id": "other"}])
    history = [
        {
            "role": "assistant",
            "content": "Five emails from Taxmann.",
            "sources": _five_taxmann_sources(),
        }
    ]
    result = _retrieve(
        question="email 9",
        history=history,
        search_emails_for_chat=search,
        get_stored_email=MagicMock(),
    )
    assert result.emails == []
    assert result.needs_clarification == (
        "That list has 5 emails. Ask for a number from 1 to 5."
    )
    search.assert_not_called()


@patch("chat_service.plan_inbox_query")
def test_taxmann_today_is_not_a_list_pick(mock_plan):
    mock_plan.return_value = InboxQueryPlan(
        sender_contains="Taxmann",
        question_kind="focused",
        use_recent_fallback=False,
    )
    search = MagicMock(return_value=[{"id": "e1", "sender": "Taxmann"}])
    history = [
        {
            "role": "assistant",
            "content": "Five emails from Taxmann.",
            "sources": _five_taxmann_sources(),
        }
    ]
    result = _retrieve(
        question="emails from Taxmann today",
        history=history,
        search_emails_for_chat=search,
        today=date(2026, 11, 5),
    )
    assert list_pick_index("emails from Taxmann today") is None
    assert result.needs_clarification is None
    search.assert_called()


if __name__ == "__main__":
    test_emails_to_sources_maps_ids()
    test_retrieve_uses_selected_email_id()
    test_retrieve_draft_reply_uses_selected_email()
    test_retrieve_draft_reply_clarifies_when_multiple()
    test_run_chat_draft_reply_saves_draft()
    test_run_chat_draft_reply_no_reply_needed()
    test_run_chat_draft_reply_clarify_includes_sources()
    test_retrieve_recent_fallback()
    test_retrieve_applies_filters()
    test_run_chat_returns_sources()
    test_last_email_returns_one_matching_source()
    test_focused_list_returns_sources_in_order()
    test_broad_summary_sources_capped_at_five()
    test_tokenize_keywords_splits_phrases()
    test_row_matches_all_tokens_requires_every_token()
    test_completed_analysis_skips_openai_when_done()
    test_retrieve_count_uses_postgres_count_not_search_cap()
    test_broad_summary_marks_truncated_when_count_exceeds_retrieved()
    test_run_chat_count_does_not_use_first_fifteen()
    test_run_chat_broad_summary_notes_truncated_coverage()
    test_classify_count_and_comparison()
    test_mail_brief_is_not_daily_briefing()
    test_format_and_run_chat_briefing()
    test_parse_ordinal_and_relative_dates()
    test_september_first_uses_sql_count_not_retrieval_limit()
    test_monthly_count_september()
    test_unread_count_sets_unread_only()
    test_sender_and_date_count()
    test_date_range_count()
    test_comparison_uses_two_counts_not_search()
    test_count_zero_is_not_empty_context_sentence()
    test_count_database_failure_is_not_swallowed()
    test_inbox_wide_count_ignores_selected_email()
    test_parse_sender_person_org_email_and_domain()
    test_equivalent_sender_month_wordings_share_filters()
    test_count_does_not_use_sender_as_body_keyword()
    test_relative_dates_use_provided_today()
    test_ambiguous_sender_asks_instead_of_guessing()
    test_conflicting_dates_ask_for_clarification()
    test_partial_and_domain_sender_counts_bypass_limit()
    test_run_chat_general_does_not_require_emails()
    test_world_cup_question_does_not_count_mailbox()
    test_email_question_without_email_keyword_searches_mailbox()
    test_followup_reuses_prior_mailbox_search()
    test_broad_summary_without_keywords_uses_full_count()
    test_yesterday_count_uses_sql_and_kolkata_day()
    test_undated_mailbox_count_floors_at_june_2026()
    test_followup_important_ones_reuses_prior_search()
    test_empty_keyword_search_is_not_saved_emails_sentence()
    test_account_isolation_passed_to_count()
    test_general_docker_question_uses_openai()
    test_email_context_uses_local_date()
    test_list_pick_phrases()
    test_email_number_returns_that_source_only()
    test_email_number_out_of_range_does_not_search()
    test_taxmann_today_is_not_a_list_pick()
    print("All chat retrieval tests passed.")
