
"""Unit tests for Gmail sync helpers, ordering, and history parsing."""

from unittest.mock import MagicMock, patch

import gmail_sync
from gmail_sync import (
    HistoryExpiredError,
    message_matches_category,
    parse_history_events,
)


def test_message_matches_category_primary_and_inbox():
    primary = ["INBOX", "CATEGORY_PERSONAL", "UNREAD"]
    promo = ["INBOX", "CATEGORY_PROMOTIONS"]
    archived = ["CATEGORY_PERSONAL"]

    assert message_matches_category(primary, "primary") is True
    assert message_matches_category(promo, "primary") is False
    assert message_matches_category(promo, "promotions") is True
    assert message_matches_category(primary, "inbox") is True
    assert message_matches_category(archived, "inbox") is False


def test_parse_history_archive_and_add():
    history = [
        {
            "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}],
            "labelsRemoved": [
                {"message": {"id": "m2"}, "labelIds": ["INBOX"]},
            ],
            "messagesDeleted": [{"message": {"id": "m3"}}],
        }
    ]
    events = parse_history_events(history)
    assert events["added_ids"] == ["m1"]
    assert "m2" in events["removed_ids"]
    assert "m3" in events["removed_ids"]


def test_list_history_expired_raises():
    from googleapiclient.errors import HttpError

    fake_resp = MagicMock()
    fake_resp.status = 404
    http_error = HttpError(fake_resp, b"historyId too old")

    fake_service = MagicMock()
    fake_request = MagicMock()
    fake_request.execute.side_effect = http_error
    fake_service.users.return_value.history.return_value.list.return_value = (
        fake_request
    )

    with patch("gmail_sync._build_service", return_value=fake_service):
        try:
            gmail_sync.list_history_page(
                object(),
                start_history_id="1",
            )
            assert False, "expected HistoryExpiredError"
        except HistoryExpiredError:
            pass


def test_conversation_grouping_order_by_internal_date():
    """Newest thread activity wins; created_at is irrelevant."""
    rows = [
        {
            "id": "e1",
            "gmail_message_id": "m1",
            "gmail_thread_id": "t-old",
            "internal_date": 100,
            "label_ids": ["INBOX", "CATEGORY_PERSONAL"],
            "subject": "Old thread",
            "sender": "a@x.com",
            "snippet": "a",
            "received_at": None,
            "analysis_status": "done",
            "email_analyses": [],
            "created_at": "2099-01-01T00:00:00Z",
        },
        {
            "id": "e2",
            "gmail_message_id": "m2",
            "gmail_thread_id": "t-new",
            "internal_date": 200,
            "label_ids": ["INBOX", "CATEGORY_PERSONAL"],
            "subject": "New thread",
            "sender": "b@x.com",
            "snippet": "b",
            "received_at": None,
            "analysis_status": "pending",
            "email_analyses": None,
            "created_at": "2000-01-01T00:00:00Z",
        },
        {
            "id": "e3",
            "gmail_message_id": "m3",
            "gmail_thread_id": "t-new",
            "internal_date": 150,
            "label_ids": ["INBOX", "CATEGORY_PERSONAL"],
            "subject": "New thread older msg",
            "sender": "b@x.com",
            "snippet": "c",
            "received_at": None,
            "analysis_status": "done",
            "email_analyses": [],
            "created_at": "2001-01-01T00:00:00Z",
        },
    ]

    # Simulate list_conversations fold (newest first input).
    rows_sorted = sorted(rows, key=lambda r: r["internal_date"], reverse=True)
    seen = set()
    conversations = []
    for row in rows_sorted:
        if not message_matches_category(row["label_ids"], "primary"):
            continue
        tid = row["gmail_thread_id"]
        if tid in seen:
            continue
        seen.add(tid)
        conversations.append(tid)

    assert conversations == ["t-new", "t-old"]


def test_upsert_idempotent_conflict_key():
    """Duplicate gmail message id for same account uses composite conflict."""
    captured = {}

    class FakeTable:
        def __init__(self):
            self._mode = "select"

        def upsert(self, payload, on_conflict=None):
            captured["data"] = payload
            captured["on_conflict"] = on_conflict
            self._mode = "upsert"
            self._payload = payload
            return self

        def select(self, *_a, **_k):
            self._mode = "select"
            return self

        def eq(self, *_a, **_k):
            return self

        def maybe_single(self):
            return self

        def execute(self):
            result = MagicMock()
            if self._mode == "upsert":
                result.data = [{**self._payload, "id": "uuid-1"}]
            else:
                result.data = None
            return result

    class FakeClient:
        def table(self, name):
            assert name == "emails"
            return FakeTable()

    email = {
        "id": "msg-1",
        "thread_id": "th-1",
        "subject": "Hi",
        "from": "a@b.com",
        "date": "x",
        "snippet": "s",
        "body": "b",
        "internal_date": 123,
        "label_ids": ["INBOX"],
        "in_inbox": True,
    }

    with patch("db.get_supabase_client", return_value=FakeClient()):
        from db import upsert_email_record

        row = upsert_email_record(email, gmail_account_email="me@example.com")

    assert captured["on_conflict"] == "gmail_account_email,gmail_message_id"
    assert captured["data"]["gmail_message_id"] == "msg-1"
    assert captured["data"]["analysis_status"] == "pending"
    assert row["id"] == "uuid-1"


if __name__ == "__main__":
    test_message_matches_category_primary_and_inbox()
    test_parse_history_archive_and_add()
    test_list_history_expired_raises()
    test_conversation_grouping_order_by_internal_date()
    test_upsert_idempotent_conflict_key()
    print("All gmail_sync tests passed.")
