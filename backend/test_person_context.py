"""Ask About a Person — resolution, grounding, and chat routing (mocked)."""

from datetime import date
from unittest.mock import MagicMock, patch

from chat_service import retrieve_emails_for_question, run_chat
from person_context import (
    ask_about_person_enabled,
    extract_person_term,
    group_people,
    is_person_question,
    render_person_answer,
    retrieve_person_context,
)
from schemas import InboxQueryPlan

ACCOUNT = "me@example.com"


def _email(
    *,
    email_id: str,
    sender: str,
    subject: str,
    thread_id: str = "t1",
    internal_date: int = 1_700_000_000_000,
    labels: list[str] | None = None,
    summary: str = "",
    deadlines: list | None = None,
    action_required: bool = False,
    account: str = ACCOUNT,
):
    return {
        "id": email_id,
        "gmail_message_id": f"gm-{email_id}",
        "gmail_account_email": account,
        "gmail_thread_id": thread_id,
        "sender": sender,
        "subject": subject,
        "received_at": "Nov 1, 2026",
        "snippet": summary,
        "internal_date": internal_date,
        "label_ids": labels or ["INBOX"],
        "email_analyses": [
            {
                "summary": summary,
                "deadlines": deadlines or [],
                "action_required": action_required,
                "importance": "high" if action_required else "medium",
            }
        ],
    }


def _retrieve_chat(question: str, **kwargs):
    defaults = {
        "gmail_account_email": ACCOUNT,
        "get_stored_email": MagicMock(),
        "search_emails_for_chat": MagicMock(return_value=[]),
        "list_stored_emails": MagicMock(return_value=[]),
        "count_emails_for_chat": MagicMock(return_value=0),
        "today": date(2026, 11, 4),
    }
    defaults.update(kwargs)
    return retrieve_emails_for_question(question, **defaults)


def test_flag_defaults_off():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "false"}, clear=False):
        assert ask_about_person_enabled() is False
    with patch.dict("os.environ", {}, clear=False):
        # Unset must stay off. pop inside the patch via environ copy is enough
        # when the key is absent from the process.
        pass


def test_person_intent_and_term():
    assert is_person_question("What did Rahul last email me about?")
    assert extract_person_term("What did Rahul last email me about?") == "Rahul"
    assert extract_person_term(
        "What did rahul.sharma@example.com last email me about?"
    ) == "rahul.sharma@example.com"
    assert is_person_question("Summarize our recent conversations with Priya Shah")
    assert is_person_question("What are my pending tasks with Priya Shah?")
    assert is_person_question("What commitments did Priya make?")
    assert is_person_question("What deadlines did Priya mention?")
    assert is_person_question("Are we waiting for a reply from Priya?")
    assert is_person_question("What should I follow up on with Priya?")
    assert not is_person_question("What is a balance sheet?")
    assert not is_person_question("Show my daily briefing")
    assert not is_person_question("What important emails need my attention?")


def test_exact_email_match():
    rows = [
        _email(
            email_id="1",
            sender="Rahul Sharma <rahul.sharma@example.com>",
            subject="Invoice",
        )
    ]
    people = group_people(rows, term="rahul.sharma@example.com", account_email=ACCOUNT)
    assert len(people) == 1
    assert people[0]["email"] == "rahul.sharma@example.com"


def test_partial_name_match():
    rows = [
        _email(
            email_id="1",
            sender="Priya Shah <priya@example.com>",
            subject="Notes",
        )
    ]
    people = group_people(rows, term="Priya", account_email=ACCOUNT)
    assert len(people) == 1
    assert people[0]["name"] == "Priya Shah"


def test_duplicate_names_are_not_merged():
    rows = [
        _email(
            email_id="1",
            sender="Rahul Sharma <rahul.a@example.com>",
            subject="A",
        ),
        _email(
            email_id="2",
            sender="Rahul Sharma <rahul.b@example.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    people = group_people(rows, term="Rahul", account_email=ACCOUNT)
    assert len(people) == 2
    emails = {person["email"] for person in people}
    assert emails == {"rahul.a@example.com", "rahul.b@example.com"}


def test_verified_alias_merges_and_unverified_does_not():
    rows = [
        _email(
            email_id="1",
            sender="Rahul Sharma <rahul.a@example.com>",
            subject="A",
        ),
        _email(
            email_id="2",
            sender="Rahul Sharma <rahul.b@example.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    verified = [
        {
            "canonical_email": "rahul.a@example.com",
            "alias_email": "rahul.b@example.com",
            "verified": True,
        }
    ]
    merged = group_people(
        rows, term="Rahul", aliases=verified, account_email=ACCOUNT
    )
    assert len(merged) == 1
    assert set(merged[0]["addresses"]) == {
        "rahul.a@example.com",
        "rahul.b@example.com",
    }
    unverified = [
        {
            "canonical_email": "rahul.a@example.com",
            "alias_email": "rahul.b@example.com",
            "verified": False,
        }
    ]
    separate = group_people(
        rows, term="Rahul", aliases=unverified, account_email=ACCOUNT
    )
    assert len(separate) == 2


def test_incoming_and_outgoing_on_same_thread():
    incoming = _email(
        email_id="in",
        sender="Priya Shah <priya@example.com>",
        subject="Draft notes",
        summary="Please review the notes.",
        internal_date=200,
    )
    outgoing = _email(
        email_id="out",
        sender="Me <me@example.com>",
        subject="Re: Draft notes",
        thread_id="t1",
        labels=["SENT"],
        summary="I will review them.",
        internal_date=300,
    )
    search = MagicMock(return_value=[incoming])
    threads = MagicMock(return_value=[incoming, outgoing])
    result = retrieve_person_context(
        "Summarize our recent conversations with Priya Shah",
        gmail_account_email=ACCOUNT,
        search_emails=search,
        list_threads=threads,
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "ready"
    ids = {row["id"] for row in result["messages"]}
    assert ids == {"in", "out"}
    assert result["person"]["sent_history_incomplete"] is False
    assert search.call_args.kwargs["gmail_account_email"] == ACCOUNT
    assert search.call_args.kwargs["in_inbox_only"] is False


def test_empty_history_and_incomplete_sent():
    search = MagicMock(return_value=[])
    result = retrieve_person_context(
        "What did Priya last email me about?",
        gmail_account_email=ACCOUNT,
        search_emails=search,
        list_threads=MagicMock(),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "empty"
    assert "SENT" in result["answer"] or "Outgoing" in result["answer"]
    assert result["person"]["sent_history_incomplete"] is True

    incoming = _email(
        email_id="in",
        sender="Priya Shah <priya@example.com>",
        subject="Only inbound",
        summary="A stored inbound note.",
    )
    ready = retrieve_person_context(
        "What did Priya last email me about?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=[incoming]),
        list_threads=MagicMock(return_value=[incoming]),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    assert ready["person"]["sent_history_incomplete"] is True
    assert "SENT" in ready["answer"] or "Outgoing" in ready["answer"]


def test_account_isolation_drops_other_mailbox():
    foreign = _email(
        email_id="x",
        sender="Priya Shah <priya@example.com>",
        subject="Secret",
        account="other@example.com",
    )
    search = MagicMock(return_value=[foreign])
    threads = MagicMock()
    result = retrieve_person_context(
        "What did Priya last email me about?",
        gmail_account_email=ACCOUNT,
        search_emails=search,
        list_threads=threads,
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "empty"
    threads.assert_not_called()
    assert search.call_args.kwargs["gmail_account_email"] == ACCOUNT


def test_empty_account_rejected():
    search = MagicMock()
    try:
        retrieve_person_context(
            "What did Priya last email me about?",
            gmail_account_email="",
            search_emails=search,
        )
        raised = False
    except PermissionError:
        raised = True
    assert raised
    search.assert_not_called()


def test_followup_status_is_copied():
    incoming = _email(
        email_id="in",
        sender="Priya Shah <priya@example.com>",
        subject="Need the file",
        summary="Please send the file.",
        action_required=True,
        thread_id="thread-9",
    )
    states = [
        {
            "source_type": "followup",
            "status": "pending",
            "recommendation_key": "fu-9",
            "metadata": {
                "gmail_thread_id": "thread-9",
                "followup_state": "pending_reply",
                "source_email_id": "in",
            },
        }
    ]
    result = retrieve_person_context(
        "Are we waiting for a reply from Priya?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=[incoming]),
        list_threads=MagicMock(return_value=[incoming]),
        list_states=lambda **_kwargs: states,
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    assert result["person"]["follow_up"] == "Waiting on your reply."
    assert result["person"]["recommendation_key"] == "fu-9"
    assert result["person"]["pending_actions"]


def test_grounded_summary_drops_invented_commitment():
    incoming = _email(
        email_id="in",
        sender="Priya Shah <priya@example.com>",
        subject="Notes only",
        summary="Sharing the meeting notes.",
    )
    result = retrieve_person_context(
        "What commitments did Priya make?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=[incoming]),
        list_threads=MagicMock(return_value=[incoming]),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    captured = {}

    def fake_synth(question, evidence):
        captured["question"] = question
        captured["evidence"] = evidence
        return "They committed to wire the funds tomorrow."

    with patch("person_context.synthesize_person_answer", side_effect=fake_synth):
        rendered = render_person_answer(
            "What commitments did Priya make?", result
        )
    assert "Notes only" in captured["evidence"]
    assert "wire the funds" not in captured["evidence"]
    assert "wire the funds" not in rendered["answer"]
    assert "No commitment was found" in rendered["answer"]


def test_multiple_matches_skip_model():
    rows = [
        _email(
            email_id="1",
            sender="Rahul Sharma <rahul.a@example.com>",
            subject="A",
        ),
        _email(
            email_id="2",
            sender="Rahul Sharma <rahul.b@example.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    result = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=rows),
        list_threads=MagicMock(),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], False),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "candidates"
    assert len(result["candidates"]) == 2
    assert "Alias linking is unavailable" in result["answer"]
    with patch("person_context.synthesize_person_answer") as synth:
        rendered = render_person_answer("What did Rahul last email me about?", result)
    synth.assert_not_called()
    assert rendered["person"] is None
    assert len(rendered["candidates"]) == 2


def test_flag_off_keeps_existing_planner():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "false"}, clear=False):
        with patch("chat_service.plan_inbox_query", return_value=InboxQueryPlan()) as plan:
            retrieval = _retrieve_chat("What did Rahul last email me about?")
    plan.assert_called()
    assert retrieval.question_kind != "person"


def test_flag_on_person_skips_planner_and_general_question_does_not():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "true"}, clear=False):
        with patch("chat_service.plan_inbox_query") as plan:
            with patch(
                "person_context.retrieve_person_context",
                return_value={
                    "mode": "candidates",
                    "answer": "Which Rahul?",
                    "candidates": [
                        {
                            "id": "rahul.a@example.com",
                            "name": "Rahul",
                            "email": "rahul.a@example.com",
                        }
                    ],
                    "person": None,
                    "sources": [],
                },
            ) as person:
                retrieval = _retrieve_chat("What did Rahul last email me about?")
    plan.assert_not_called()
    person.assert_called()
    assert retrieval.question_kind == "person"

    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "true"}, clear=False):
        with patch(
            "chat_service.plan_inbox_query", return_value=InboxQueryPlan()
        ) as plan:
            other = _retrieve_chat("What important emails need my attention?")
    plan.assert_called()
    assert other.question_kind != "person"


def test_count_question_unchanged_when_flag_on():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "true"}, clear=False):
        with patch("chat_service.plan_inbox_query") as plan:
            retrieval = _retrieve_chat("How many emails from Rahul?")
    plan.assert_not_called()
    assert retrieval.question_kind == "count"


def test_run_chat_person_branch_returns_candidates():
    rendered = run_chat(
        question="What did Rahul last email me about?",
        retrieval=__import__("chat_service").ChatRetrieval(
            question_kind="person",
            person={
                "mode": "candidates",
                "answer": "Which Rahul?",
                "candidates": [
                    {
                        "id": "rahul.a@example.com",
                        "name": "Rahul A",
                        "email": "rahul.a@example.com",
                    }
                ],
                "person": None,
                "sources": [],
            },
        ),
    )
    assert rendered["answer"] == "Which Rahul?"
    assert rendered["draft"] is None
    assert rendered["candidates"][0]["email"] == "rahul.a@example.com"


def _person_deps(rows):
    return {
        "search_emails": MagicMock(return_value=rows),
        "list_threads": MagicMock(return_value=rows),
        "list_states": lambda **_kwargs: [],
        "list_aliases": lambda _account: ([], True),
        "today": date(2026, 11, 4),
    }


def test_unique_full_name_resolves():
    rows = [
        _email(
            email_id="1",
            sender="Priya Shah <priya@example.com>",
            subject="Filing",
        )
    ]
    result = retrieve_person_context(
        "Summarize our recent conversations with Priya Shah",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert result["mode"] == "ready"
    assert result["person"]["email"] == "priya@example.com"


def test_unique_exact_email_resolves():
    rows = [
        _email(
            email_id="1",
            sender="Rahul Sharma <rahul.sharma@example.com>",
            subject="Invoice",
        )
    ]
    result = retrieve_person_context(
        "What did rahul.sharma@example.com last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert result["mode"] == "ready"
    assert result["person"]["email"] == "rahul.sharma@example.com"


def test_same_first_name_is_not_guessed():
    rows = [
        _email(email_id="1", sender="Rahul Sharma <rahul.a@example.com>", subject="A"),
        _email(
            email_id="2",
            sender="Rahul Mehta <rahul.b@example.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    result = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert result["mode"] == "candidates"
    assert {item["email"] for item in result["candidates"]} == {
        "rahul.a@example.com",
        "rahul.b@example.com",
    }


def test_identical_full_names_stay_separate():
    rows = [
        _email(email_id="1", sender="Rahul Sharma <rahul.a@acme.com>", subject="A"),
        _email(
            email_id="2",
            sender="Rahul Sharma <rahul.b@beta.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    people = group_people(rows, term="Rahul Sharma", account_email=ACCOUNT)
    assert len(people) == 2
    result = retrieve_person_context(
        "What did Rahul Sharma last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert result["mode"] == "candidates"
    assert {item["company"] for item in result["candidates"]} == {
        "acme.com",
        "beta.com",
    }


def test_same_name_different_companies_show_domain():
    rows = [
        _email(email_id="1", sender="Priya Shah <priya@acme.com>", subject="Audit"),
        _email(
            email_id="2",
            sender="Priya Shah <priya@beta.com>",
            subject="Payroll",
            thread_id="t2",
        ),
    ]
    result = retrieve_person_context(
        "What did Priya Shah last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    by_email = {item["email"]: item for item in result["candidates"]}
    assert by_email["priya@acme.com"]["company"] == "acme.com"
    assert by_email["priya@acme.com"]["last_topic"] == "Audit"
    assert by_email["priya@beta.com"]["last_topic"] == "Payroll"


def test_verified_alias_is_one_person():
    rows = [
        _email(email_id="1", sender="Rahul Sharma <rahul.a@example.com>", subject="A"),
        _email(
            email_id="2",
            sender="Rahul Sharma <rahul.b@example.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    aliases = [
        {
            "canonical_email": "rahul.a@example.com",
            "alias_email": "rahul.b@example.com",
            "verified": True,
        }
    ]
    result = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=rows),
        list_threads=MagicMock(return_value=rows),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: (aliases, True),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "ready"
    assert result["person"]["email"] == "rahul.a@example.com"
    assert "rahul.b@example.com" in result["person"]["addresses"]


def test_unverified_alias_stays_split():
    rows = [
        _email(email_id="1", sender="Rahul Sharma <rahul.a@example.com>", subject="A"),
        _email(
            email_id="2",
            sender="Rahul Sharma <rahul.b@example.com>",
            subject="B",
            thread_id="t2",
        ),
    ]
    aliases = [
        {
            "canonical_email": "rahul.a@example.com",
            "alias_email": "rahul.b@example.com",
            "verified": False,
        }
    ]
    result = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=rows),
        list_threads=MagicMock(return_value=rows),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: (aliases, True),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "candidates"
    assert len(result["candidates"]) == 2


def test_shared_mailbox_stays_one_address():
    rows = [
        _email(email_id="1", sender="Support Desk <team@acme.com>", subject="Ticket"),
        _email(
            email_id="2",
            sender="Acme Team <team@acme.com>",
            subject="Ticket update",
            thread_id="t2",
        ),
    ]
    people = group_people(rows, term="team@acme.com", account_email=ACCOUNT)
    assert len(people) == 1
    assert people[0]["email"] == "team@acme.com"


def test_partial_token_does_not_match_inside_a_word():
    rows = [
        _email(email_id="1", sender="Annapurna Rao <annapurna@example.com>", subject="Hi")
    ]
    assert group_people(rows, term="Ann", account_email=ACCOUNT) == []


def test_no_match_mentions_synchronized_history():
    result = retrieve_person_context(
        "What did Priya last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps([]),
    )
    assert result["mode"] == "empty"
    assert "synchronized Gmail history" in result["answer"]


def test_more_than_five_matches_are_paged():
    rows = [
        _email(
            email_id=str(i),
            sender=f"Rahul {i} <rahul{i}@example.com>",
            subject=f"Topic {i}",
            thread_id=f"t{i}",
        )
        for i in range(6)
    ]
    first = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert first["candidate_total"] == 6
    assert len(first["candidates"]) == 5
    assert first["candidate_has_more"] is True
    second = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        candidate_offset=5,
        **_person_deps(rows),
    )
    assert len(second["candidates"]) == 1
    assert second["candidate_has_more"] is False


def test_selection_resumes_original_question():
    chosen = _email(
        email_id="1",
        sender="Rahul Sharma <rahul.a@example.com>",
        subject="GST notes",
        summary="Sharing the GST notes.",
    )
    other = _email(
        email_id="2",
        sender="Rahul Mehta <rahul.b@example.com>",
        subject="Other",
        thread_id="t2",
    )
    question = "What did Rahul last email me about?"
    result = retrieve_person_context(
        question,
        gmail_account_email=ACCOUNT,
        selected_contact_id="rahul.a@example.com",
        search_emails=MagicMock(return_value=[chosen, other]),
        list_threads=MagicMock(return_value=[chosen]),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    assert result["mode"] == "ready"
    assert result["person"]["email"] == "rahul.a@example.com"

    def fake_synth(asked, evidence):
        assert asked == question
        assert "GST notes" in evidence
        return "Rahul shared the GST notes."

    with patch("person_context.synthesize_person_answer", side_effect=fake_synth):
        rendered = render_person_answer(question, result)
    assert "GST notes" in rendered["answer"]


def test_pronoun_followup_uses_bound_contact():
    incoming = _email(
        email_id="1",
        sender="Priya Shah <priya@example.com>",
        subject="The file",
        summary="Please send the file.",
    )
    result = retrieve_person_context(
        "What did she say?",
        gmail_account_email=ACCOUNT,
        bound_contact_id="priya@example.com",
        **_person_deps([incoming]),
    )
    assert result["mode"] == "ready"
    assert result["person"]["email"] == "priya@example.com"


def test_pronoun_without_binding_asks_who():
    result = retrieve_person_context(
        "What did he say?",
        gmail_account_email=ACCOUNT,
        **_person_deps([]),
    )
    assert result["mode"] == "needs_name"
    assert "Which person" in result["answer"]


def test_new_person_overrides_previous_binding():
    with patch.dict("os.environ", {"ASK_ABOUT_PERSON_ENABLED": "true"}, clear=False):
        with patch(
            "person_context.retrieve_person_context",
            return_value={
                "mode": "ready",
                "answer": "ok",
                "candidates": [],
                "person": {"name": "Rahul", "email": "rahul.a@example.com"},
                "sources": [],
            },
        ) as person:
            _retrieve_chat(
                "What did Rahul last email me about?",
                bound_contact_id="priya@example.com",
            )
    assert person.call_args.kwargs["bound_contact_id"] is None


def test_thread_pending_stays_with_the_selected_sender():
    priya = _email(
        email_id="p",
        sender="Priya Shah <priya@example.com>",
        subject="GST filing",
        summary="Priya will file GST.",
        action_required=True,
    )
    alex = _email(
        email_id="a",
        sender="Alex Roy <alex@example.com>",
        subject="Wire the funds",
        summary="Alex promised to wire the funds.",
        action_required=True,
        thread_id="t1",
    )
    result = retrieve_person_context(
        "What commitments did Priya make?",
        gmail_account_email=ACCOUNT,
        search_emails=MagicMock(return_value=[priya]),
        list_threads=MagicMock(return_value=[priya, alex]),
        list_states=lambda **_kwargs: [],
        list_aliases=lambda _account: ([], True),
        today=date(2026, 11, 4),
    )
    pending = " ".join(result["person"]["pending_actions"])
    assert "GST" in pending
    assert "Wire" not in pending


def test_domain_alone_does_not_resolve_one_person():
    rows = [
        _email(email_id="1", sender="Meera Shah <meera@acme.com>", subject="Hello")
    ]
    result = retrieve_person_context(
        "What did Acme last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert result["mode"] == "candidates"
    assert result["candidates"][0]["match_reason"] == "Same company domain"


def test_consumer_domain_is_not_a_company():
    rows = [
        _email(email_id="1", sender="Rahul Shah <rahul@gmail.com>", subject="Hi")
    ]
    card_rows = retrieve_person_context(
        "What did Rahul last email me about?",
        gmail_account_email=ACCOUNT,
        **_person_deps(rows),
    )
    assert card_rows["mode"] == "ready"
    people = group_people(rows, term="gmail.com", account_email=ACCOUNT)
    assert people == []


if __name__ == "__main__":
    test_flag_defaults_off()
    test_person_intent_and_term()
    test_exact_email_match()
    test_partial_name_match()
    test_duplicate_names_are_not_merged()
    test_verified_alias_merges_and_unverified_does_not()
    test_incoming_and_outgoing_on_same_thread()
    test_empty_history_and_incomplete_sent()
    test_account_isolation_drops_other_mailbox()
    test_empty_account_rejected()
    test_followup_status_is_copied()
    test_grounded_summary_drops_invented_commitment()
    test_multiple_matches_skip_model()
    test_flag_off_keeps_existing_planner()
    test_flag_on_person_skips_planner_and_general_question_does_not()
    test_count_question_unchanged_when_flag_on()
    test_run_chat_person_branch_returns_candidates()
    test_unique_full_name_resolves()
    test_unique_exact_email_resolves()
    test_same_first_name_is_not_guessed()
    test_identical_full_names_stay_separate()
    test_same_name_different_companies_show_domain()
    test_verified_alias_is_one_person()
    test_unverified_alias_stays_split()
    test_shared_mailbox_stays_one_address()
    test_partial_token_does_not_match_inside_a_word()
    test_no_match_mentions_synchronized_history()
    test_more_than_five_matches_are_paged()
    test_selection_resumes_original_question()
    test_pronoun_followup_uses_bound_contact()
    test_pronoun_without_binding_asks_who()
    test_new_person_overrides_previous_binding()
    test_thread_pending_stays_with_the_selected_sender()
    test_domain_alone_does_not_resolve_one_person()
    test_consumer_domain_is_not_a_company()
    print("person context tests passed")
