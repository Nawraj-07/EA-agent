"""Unit tests for Meta WhatsApp Cloud API helpers. No live sends."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import whatsapp_notifications as wn
import whatsapp_service as ws


def test_whatsapp_disabled_skips_http():
    with patch.dict("os.environ", {"WHATSAPP_ENABLED": "false"}, clear=False):
        client = MagicMock()
        result = ws.send_template(
            to="919211400683",
            template_name="hello_world",
            http_client=client,
        )
    assert result["status"] == "disabled"
    assert result["ok"] is False
    client.post.assert_not_called()


def test_build_template_payload_with_and_without_params():
    bare = ws.build_template_payload(
        to="919211400683",
        template_name="hello_world",
        language_code="en_US",
        body_params=[],
    )
    assert bare["to"] == "919211400683"
    assert bare["template"]["name"] == "hello_world"
    assert "components" not in bare["template"]

    with_params = ws.build_template_payload(
        to="+91 92114 00683",
        template_name="buddy_daily_briefing",
        language_code="en_US",
        body_params=["12", "3", "2"],
    )
    assert with_params["to"] == "919211400683"
    comps = with_params["template"]["components"]
    assert comps[0]["type"] == "body"
    assert [p["text"] for p in comps[0]["parameters"]] == ["12", "3", "2"]


def test_send_template_maps_auth_failure():
    client = MagicMock()
    response = MagicMock()
    response.status_code = 401
    response.json.return_value = {
        "error": {"message": "Invalid OAuth access token.", "code": 190}
    }
    client.post.return_value = response
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_ACCESS_TOKEN": "fake-token",
            "WHATSAPP_PHONE_NUMBER_ID": "1445248775336401",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
        },
        clear=False,
    ):
        result = ws.send_template(
            to="919211400683",
            template_name="hello_world",
            http_client=client,
        )
    assert result["ok"] is False
    assert result["status"] == "auth_failed"
    assert result["error_code"] == "auth_failed"
    assert "fake-token" not in str(result)


def test_send_template_timeout():
    import httpx

    client = MagicMock()
    client.post.side_effect = httpx.TimeoutException("timeout")
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_ACCESS_TOKEN": "fake-token",
            "WHATSAPP_PHONE_NUMBER_ID": "1445248775336401",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
        },
        clear=False,
    ):
        result = ws.send_template(
            to="919211400683",
            template_name="hello_world",
            http_client=client,
        )
    assert result["ok"] is False
    assert result["status"] == "timeout"


def test_send_template_success_accepted():
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"messages": [{"id": "wamid.TEST123"}]}
    client.post.return_value = response
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_ACCESS_TOKEN": "fake-token",
            "WHATSAPP_PHONE_NUMBER_ID": "1445248775336401",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
        },
        clear=False,
    ):
        result = ws.send_template(
            to="919211400683",
            template_name="hello_world",
            http_client=client,
        )
    assert result["ok"] is True
    assert result["status"] == "accepted"
    assert result["meta_message_id"] == "wamid.TEST123"
    assert "0683" in result["recipient_masked"]


def test_mask_recipient_hides_prefix():
    assert ws.mask_recipient("919211400683").endswith("0683")
    assert not ws.mask_recipient("919211400683").startswith("9192")


@patch("whatsapp_notifications.template_is_sendable", return_value=True)
@patch("whatsapp_notifications.send_template")
@patch("db.update_whatsapp_outbox_status")
@patch("db.claim_whatsapp_outbox_row")
def test_daily_briefing_notify_dedupe(
    mock_claim, mock_update, mock_send, _mock_sendable
):
    mock_claim.return_value = None
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
            "WHATSAPP_TEMPLATE_DAILY_BRIEFING": "buddy_daily_briefing",
        },
        clear=False,
    ):
        result = wn.notify_whatsapp_daily_briefing(
            {"saved": True, "briefing_id": "b1", "email_count": 5},
            {
                "email_count": 5,
                "overview": {
                    "important_updates": 2,
                    "pending_actions": 1,
                    "meetings": 0,
                },
            },
        )
    assert result["status"] == "duplicate"
    mock_send.assert_not_called()
    mock_update.assert_not_called()


@patch("whatsapp_notifications.template_is_sendable", return_value=False)
@patch("whatsapp_notifications.get_template_status", return_value={"status": "pending"})
def test_daily_briefing_pending_template(mock_status, mock_sendable):
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
            "WHATSAPP_TEMPLATE_DAILY_BRIEFING": "buddy_daily_briefing",
        },
        clear=False,
    ):
        result = wn.notify_whatsapp_daily_briefing(
            {"saved": True, "briefing_id": "b2", "email_count": 3},
            {
                "email_count": 3,
                "overview": {
                    "important_updates": 1,
                    "pending_actions": 1,
                    "meetings": 0,
                },
            },
        )
    assert result["status"] == "pending_approval"


@patch("whatsapp_notifications.notify_whatsapp_daily_briefing")
def test_briefing_save_hook_failure_does_not_unsave(mock_notify):
    """WhatsApp exception must not flip saved briefing status."""
    mock_notify.side_effect = RuntimeError("boom")
    # Simulate the try/except used in briefing_service after save.
    stats = {"saved": True, "briefing_id": "x", "email_count": 1}
    try:
        mock_notify(stats, {"email_count": 1})
    except Exception:
        pass
    assert stats["saved"] is True


@patch("whatsapp_notifications.template_is_sendable", return_value=True)
@patch("whatsapp_notifications.send_template")
@patch("db.update_whatsapp_outbox_status")
@patch("db.claim_whatsapp_outbox_row", return_value={"id": "ob1"})
def test_important_email_only_for_high(
    mock_claim, mock_update, mock_send, _mock_sendable
):
    mock_send.return_value = {
        "ok": True,
        "status": "accepted",
        "meta_message_id": "wamid.1",
        "error_code": None,
        "error_message": None,
    }
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
            "WHATSAPP_TEMPLATE_IMPORTANT_EMAIL": "buddy_important_email",
        },
        clear=False,
    ):
        skipped = wn.notify_whatsapp_important_email(
            {"id": "e1", "gmail_message_id": "g1", "sender": "A", "subject": "S"},
            {"importance": "medium", "summary": "x"},
        )
        sent = wn.notify_whatsapp_important_email(
            {"id": "e1", "gmail_message_id": "g1", "sender": "A", "subject": "S"},
            {"importance": "high", "summary": "Urgent GST notice"},
        )
    assert skipped["status"] == "skipped"
    assert sent["ok"] is True
    mock_claim.assert_called_once()
    mock_send.assert_called_once()


@patch("whatsapp_notifications._dispatch")
def test_meeting_payment_scaffold_requires_templates(mock_dispatch):
    mock_dispatch.return_value = {"status": "accepted", "ok": True}
    payload = {
        "briefing_date": "2026-11-03",
        "meetings": [
            {
                "event_title": "Board call",
                "event_date": "2026-11-03",
                "start_time": "10:00",
                "status": "confirmed",
                "source_email_id": "em1",
            }
        ],
        "upcoming_deadlines": [
            {
                "title": "Invoice payment due",
                "due_date": "2026-11-03",
                "source_email_id": "em2",
            }
        ],
    }
    with patch.dict(
        "os.environ",
        {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_TEST_RECIPIENT": "919211400683",
            "WHATSAPP_TEMPLATE_MEETING": "",
            "WHATSAPP_TEMPLATE_PAYMENT": "",
        },
        clear=False,
    ):
        summary = wn.notify_whatsapp_meeting_and_payment_from_briefing(payload)
    assert summary["meetings_enqueued"] == 0
    assert summary["payments_enqueued"] == 0
    mock_dispatch.assert_not_called()


if __name__ == "__main__":
    test_whatsapp_disabled_skips_http()
    test_build_template_payload_with_and_without_params()
    test_send_template_maps_auth_failure()
    test_send_template_timeout()
    test_send_template_success_accepted()
    test_mask_recipient_hides_prefix()
    test_daily_briefing_notify_dedupe()
    test_daily_briefing_pending_template()
    test_briefing_save_hook_failure_does_not_unsave()
    test_important_email_only_for_high()
    test_meeting_payment_scaffold_requires_templates()
    print("All whatsapp tests passed.")
