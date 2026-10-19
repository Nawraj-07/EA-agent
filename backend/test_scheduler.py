
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
    test_scheduler_safe_when_gmail_auth_missing()
    test_scheduler_disabled_when_flag_false()
    print("All scheduler tests passed.")
