
"""Mocked unit tests for Gmail sync job (no real Gmail / OpenAI calls)."""

from unittest.mock import patch

import scheduler


class _FakeAnalysis:
    def model_dump(self):
        return {
            "summary": "Test summary",
            "category": "work",
            "importance": "medium",
            "deadlines": [],
            "action_required": True,
            "confidence": 0.9,
        }


def test_scheduler_backfill_ingests_without_blocking_on_analyze_failure():
    fake_creds = object()

    def fake_fetch(_creds, message_id: str):
        return {
            "id": message_id,
            "thread_id": f"t-{message_id}",
            "subject": "Hello",
            "from": "a@example.com",
            "body": "Body",
            "snippet": "Body",
            "date": "today",
            "internal_date": 1000,
            "label_ids": ["INBOX", "CATEGORY_PERSONAL"],
            "in_inbox": True,
        }

    with (
        patch("main.load_gmail_credentials", return_value=fake_creds),
        patch("scheduler.get_profile_email", return_value="me@example.com"),
        patch("scheduler.get_sync_state", return_value=None),
        patch("scheduler.ingest_newest_inbox"),
        patch("scheduler.ingest_recent_inbox"),
        patch("scheduler.reconcile_missing_inbox"),
        patch(
            "scheduler.list_messages_page",
            return_value={
                "message_ids": ["msg-new", "msg-fail"],
                "next_page_token": None,
            },
        ),
        patch("scheduler.get_profile_history_id", return_value="hist-1"),
        patch("scheduler.upsert_sync_state"),
        patch("scheduler.fetch_message_for_ingest", side_effect=fake_fetch),
        patch("scheduler.upsert_email_record") as mock_upsert,
        patch(
            "scheduler.list_pending_analysis_emails",
            return_value=[
                {
                    "id": "uuid-1",
                    "subject": "Hello",
                    "sender": "a@example.com",
                    "body": "Body",
                },
                {
                    "id": "uuid-2",
                    "subject": "Fail",
                    "sender": "a@example.com",
                    "body": "Body",
                },
            ],
        ),
        patch(
            "scheduler.analyze_email_content",
            side_effect=[_FakeAnalysis(), RuntimeError("openai down")],
        ),
        patch("scheduler.save_analysis_for_email") as mock_save_analysis,
        patch("scheduler.mark_analysis_failed") as mock_fail,
    ):
        stats = scheduler.sync_gmail_inbox()

    assert stats["mode"] == "backfill"
    assert stats["saved"] == 2
    assert stats["failed"] == 0
    assert stats["analyzed"] == 1
    assert stats["analyze_failed"] == 1
    assert mock_upsert.call_count == 2
    mock_save_analysis.assert_called_once()
    mock_fail.assert_called_once_with("uuid-2")


def test_scheduler_history_archives_removed_inbox():
    fake_creds = object()

    with (
        patch("main.load_gmail_credentials", return_value=fake_creds),
        patch("scheduler.get_profile_email", return_value="me@example.com"),
        patch("scheduler.ingest_newest_inbox"),
        patch("scheduler.ingest_recent_inbox"),
        patch("scheduler.reconcile_missing_inbox"),
        patch(
            "scheduler.get_sync_state",
            return_value={
                "backfill_status": "complete",
                "history_id": "10",
            },
        ),
        patch(
            "scheduler.list_history_page",
            return_value={
                "history": [
                    {
                        "labelsRemoved": [
                            {"message": {"id": "archived-1"}, "labelIds": ["INBOX"]}
                        ]
                    }
                ],
                "next_page_token": None,
                "history_id": "11",
            },
        ),
        patch("scheduler.parse_history_events", wraps=scheduler.parse_history_events),
        patch("scheduler.mark_email_not_in_inbox") as mock_archive,
        patch("scheduler.upsert_sync_state"),
        patch(
            "scheduler.list_pending_analysis_emails",
            return_value=[],
        ),
        patch("scheduler.fetch_message_for_ingest"),
        patch("scheduler.upsert_email_record"),
    ):
        stats = scheduler.sync_gmail_inbox()

    assert stats["mode"] == "history"
    assert stats["archived"] == 1
    mock_archive.assert_called_once_with(
        "archived-1",
        gmail_account_email="me@example.com",
    )


def test_history_during_backfill_does_not_clear_page_token():
    fake_creds = object()
    upsert_calls: list[dict] = []

    def capture_upsert(_account, **kwargs):
        upsert_calls.append(kwargs)
        return {"gmail_account_email": "me@example.com", **kwargs}

    with (
        patch("main.load_gmail_credentials", return_value=fake_creds),
        patch("scheduler.get_profile_email", return_value="me@example.com"),
        patch("scheduler.ingest_newest_inbox"),
        patch("scheduler.ingest_recent_inbox"),
        patch("scheduler.reconcile_missing_inbox"),
        patch(
            "scheduler.get_sync_state",
            return_value={
                "backfill_status": "running",
                "history_id": "10",
                "backfill_page_token": "page-4",
            },
        ),
        patch(
            "scheduler.list_history_page",
            return_value={
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "mid-gap", "labelIds": ["INBOX"]}}
                        ]
                    }
                ],
                "next_page_token": None,
                "history_id": "11",
            },
        ),
        patch(
            "scheduler.list_messages_page",
            return_value={
                "message_ids": ["older-1"],
                "next_page_token": "page-5",
            },
        ),
        patch("scheduler.fetch_message_for_ingest", return_value={"id": "x", "in_inbox": True}),
        patch("scheduler.upsert_email_record"),
        patch("scheduler.upsert_sync_state", side_effect=capture_upsert),
        patch("scheduler.list_pending_analysis_emails", return_value=[]),
        patch("scheduler.get_profile_history_id", return_value="11"),
    ):
        stats = scheduler.sync_gmail_inbox()

    assert stats["mode"] == "backfill"
    history_upserts = [
        call
        for call in upsert_calls
        if call.get("history_id") == "11" and call.get("clear_page_token")
    ]
    assert history_upserts == []
    assert any(call.get("backfill_page_token") == "page-5" for call in upsert_calls)


def test_reconcile_ingests_only_missing_ids():
    stats = {
        "checked": 0,
        "saved": 0,
        "failed": 0,
        "reconcile_scanned": 0,
        "reconcile_missing": 0,
    }
    ingested: list[str] = []

    with (
        patch(
            "scheduler.list_messages_page",
            return_value={
                "message_ids": ["have-1", "miss-1", "have-2"],
                "next_page_token": None,
            },
        ),
        patch(
            "scheduler.existing_gmail_message_ids",
            return_value={"have-1", "have-2"},
        ),
        patch(
            "scheduler._ingest_message_ids",
            side_effect=lambda _c, _a, ids, _s: ingested.extend(ids),
        ),
    ):
        scheduler.reconcile_missing_inbox(object(), "me@example.com", stats, max_pages=1)

    assert stats["reconcile_scanned"] == 3
    assert stats["reconcile_missing"] == 1
    assert ingested == ["miss-1"]


def test_ingest_retries_then_succeeds():
    stats = {"checked": 0, "saved": 0, "failed": 0}
    fetches = {"n": 0}

    def flaky(_creds, _mid):
        fetches["n"] += 1
        if fetches["n"] < 2:
            raise RuntimeError("transient")
        return {"id": "m1", "in_inbox": True}

    with (
        patch("scheduler.fetch_message_for_ingest", side_effect=flaky),
        patch("scheduler.upsert_email_record"),
        patch("scheduler.time.sleep"),
    ):
        scheduler._ingest_message_ids(object(), "me@example.com", ["m1"], stats)

    assert fetches["n"] == 2
    assert stats["saved"] == 1
    assert stats["failed"] == 0


def test_sync_runs_unread_catchup_and_reconcile():
    fake_creds = object()

    with (
        patch("main.load_gmail_credentials", return_value=fake_creds),
        patch("scheduler.get_profile_email", return_value="me@example.com"),
        patch(
            "scheduler.get_sync_state",
            return_value={"backfill_status": "complete", "history_id": "10"},
        ),
        patch("scheduler.ingest_newest_inbox"),
        patch("scheduler.ingest_recent_inbox") as mock_recent,
        patch("scheduler.reconcile_missing_inbox") as mock_reconcile,
        patch("scheduler._run_history_incremental"),
        patch("scheduler.list_pending_analysis_emails", return_value=[]),
        patch("scheduler.upsert_sync_state"),
    ):
        scheduler.sync_gmail_inbox()

    assert mock_reconcile.call_count == 1
    queries = [
        call.kwargs.get("query")
        for call in mock_recent.call_args_list
        if call.kwargs.get("query")
    ]
    assert "in:inbox is:unread" in queries
    assert mock_recent.call_count == 2


def test_analyze_pending_can_filter_pending_only():
    with patch("scheduler.list_pending_analysis_emails", return_value=[]) as mock_list:
        scheduler.analyze_pending_emails(
            gmail_account_email="me@example.com",
            statuses=["pending"],
        )

    assert mock_list.call_args.kwargs["statuses"] == ["pending"]
    assert mock_list.call_args.kwargs["gmail_account_email"] == "me@example.com"


def test_scheduler_safe_when_gmail_auth_missing():
    from fastapi import HTTPException

    with patch(
        "main.load_gmail_credentials",
        side_effect=HTTPException(status_code=401, detail="Gmail not connected"),
    ):
        stats = scheduler.sync_gmail_inbox()

    assert stats["checked"] == 0
    assert stats["saved"] == 0


def test_scheduler_disabled_when_flag_false():
    with patch.dict(
        "os.environ",
        {"ENABLE_SCHEDULER": "false"},
        clear=False,
    ):
        assert scheduler.scheduler_enabled() is False


if __name__ == "__main__":
    test_scheduler_backfill_ingests_without_blocking_on_analyze_failure()
    test_scheduler_history_archives_removed_inbox()
    test_history_during_backfill_does_not_clear_page_token()
    test_reconcile_ingests_only_missing_ids()
    test_ingest_retries_then_succeeds()
    test_sync_runs_unread_catchup_and_reconcile()
    test_analyze_pending_can_filter_pending_only()
    test_scheduler_safe_when_gmail_auth_missing()
    test_scheduler_disabled_when_flag_false()
    print("All scheduler tests passed.")
