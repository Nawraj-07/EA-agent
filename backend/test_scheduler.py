
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


def _fake_fetch(message_id: str):
    if message_id == "msg-fail":
        raise RuntimeError("boom")
    return {
        "id": message_id,
        "subject": "Hello",
        "from": "a@example.com",
        "body": "Body",
        "snippet": "Body",
        "date": "today",
    }


def test_scheduler_skips_existing_and_saves_new():
    fake_creds = object()

    with (
        patch("main.load_gmail_credentials", return_value=fake_creds),
        patch(
            "scheduler._list_recent_inbox_message_ids",
            return_value=["msg-existing", "msg-new", "msg-fail"],
        ),
        patch(
            "scheduler.email_exists_by_gmail_id",
            side_effect=lambda mid: mid == "msg-existing",
        ),
        patch("main.fetch_email_by_id", side_effect=_fake_fetch),
        patch(
            "scheduler.analyze_email_content",
            return_value=_FakeAnalysis(),
        ) as mock_analyze,
        patch("scheduler.save_email_and_analysis") as mock_save,
    ):
        stats = scheduler.sync_gmail_inbox()

    assert stats == {
        "checked": 3,
        "skipped": 1,
        "saved": 1,
        "failed": 1,
    }
    mock_analyze.assert_called_once()
    mock_save.assert_called_once()


def test_scheduler_safe_when_gmail_auth_missing():
    from fastapi import HTTPException

    with patch(
        "main.load_gmail_credentials",
        side_effect=HTTPException(status_code=401, detail="Gmail not connected"),
    ):
        stats = scheduler.sync_gmail_inbox()

    assert stats == {
        "checked": 0,
        "skipped": 0,
        "saved": 0,
        "failed": 0,
    }


def test_scheduler_disabled_when_flag_false():
    with patch.dict(
        "os.environ",
        {"ENABLE_SCHEDULER": "false"},
        clear=False,
    ):
        # PYTEST_CURRENT_TEST also forces disabled while under pytest.
        assert scheduler.scheduler_enabled() is False


if __name__ == "__main__":
    test_scheduler_skips_existing_and_saves_new()
    test_scheduler_safe_when_gmail_auth_missing()
    test_scheduler_disabled_when_flag_false()
    print("All scheduler tests passed.")
