"""Tests for End My Day closing review."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from end_my_day_service import (
    build_review,
    classify_reply_need,
    clear_review_cache,
    end_my_day_enabled,
    finish_my_day,
    read_review_cache,
    refresh_mailbox,
    store_review_cache,
    today_window,
    _sync_freshness,
)


ACCOUNT = "user@example.com"
OTHER = "other@example.com"
NOW = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)


def _email(
    *,
    email_id: str,
    sender: str,
    subject: str,
    action_required: bool | None = None,
    importance: str = "high",
    category: str = "work",
    deadlines: list | None = None,
    labels: list | None = None,
    summary: str = "Needs a decision.",
) -> dict:
    analysis = {
        "importance": importance,
        "summary": summary,
        "category": category,
        "confidence": 0.9,
        "deadlines": deadlines or [],
    }
    if action_required is not None:
        analysis["action_required"] = action_required
    return {
        "id": email_id,
        "gmail_message_id": email_id,
        "gmail_thread_id": f"t-{email_id}",
        "sender": sender,
        "subject": subject,
        "snippet": summary,
        "body": summary,
        "internal_date": 1,
        "label_ids": labels or ["INBOX"],
        "email_analyses": [analysis],
    }


def _item(email: dict, *, state: str, key: str) -> dict:
    return {
        "recommendation_key": key,
        "source_email_id": email["id"],
        "gmail_thread_id": email["gmail_thread_id"],
        "subject": email["subject"],
        "title": email["subject"],
        "sender": email["sender"],
        "recipient": "client@example.com",
        "priority": "high",
        "waiting_hours": 5,
        "followup_state": state,
    }


def _enabled():
    return patch.dict("os.environ", {"END_MY_DAY_ENABLED": "true"}, clear=False)


def _kolkata():
    return patch.dict("os.environ", {"BRIEFING_TIMEZONE": "Asia/Kolkata"}, clear=False)


def test_flag_default_off():
    with patch.dict("os.environ", {"END_MY_DAY_ENABLED": "false"}, clear=False):
        assert end_my_day_enabled() is False


def test_disabled_does_not_load_mail():
    def explode(**_kwargs):
        raise AssertionError("mail should not be loaded")

    with patch.dict("os.environ", {"END_MY_DAY_ENABLED": "false"}, clear=False):
        out = build_review(account_email=ACCOUNT, load_emails=explode)
    assert out["enabled"] is False


def test_timezone_boundary_uses_local_date():
    late_utc = datetime(2026, 11, 5, 20, 30, tzinfo=timezone.utc)
    with _kolkata():
        window = today_window(late_utc)
    assert window["local_date"] == "2026-11-06"
    assert window["timezone"] == "Asia/Kolkata"


def _sample_mail():
    incoming = _email(
        email_id="in-ok",
        sender="Client <boss@co.com>",
        subject="Please review the filing",
        action_required=True,
        deadlines=["2026-11-06", "2026-11-04"],
    )
    outgoing = _email(
        email_id="out-ok",
        sender=ACCOUNT,
        subject="Please send the signed copy",
        action_required=True,
        labels=["SENT"],
    )
    expired = _email(
        email_id="expired",
        sender="Alex <alex@co.com>",
        subject="Please send the signed agreement",
        action_required=True,
    )
    dismissed = _email(
        email_id="dismissed",
        sender="Sam <sam@co.com>",
        subject="Please confirm the invoice",
        action_required=True,
    )
    snoozed = _email(
        email_id="snoozed",
        sender="Rae <rae@co.com>",
        subject="Please reply on the contract",
        action_required=True,
    )
    completed = _email(
        email_id="completed",
        sender="Jo <jo@co.com>",
        subject="Please send the receipt",
        action_required=True,
    )
    promo = _email(
        email_id="promo",
        sender="Weekly <newsletter@news.com>",
        subject="This week's newsletter",
        action_required=True,
        category="promotions",
    )
    webinar = _email(
        email_id="webinar",
        sender="Events <events@co.com>",
        subject="Join our webinar on filings",
        action_required=True,
    )
    info = _email(
        email_id="info",
        sender="Notes <notes@co.com>",
        subject="FYI office update",
        action_required=False,
        importance="low",
        category="other",
        summary="No action needed.",
    )
    unresolved = _email(
        email_id="open-item",
        sender="Priya <priya@co.com>",
        subject="Approve the vendor payment",
        action_required=True,
        importance="high",
    )
    emails = [
        incoming,
        outgoing,
        expired,
        dismissed,
        snoozed,
        completed,
        promo,
        webinar,
        info,
        unresolved,
    ]
    active = [
        _item(incoming, state="pending_reply", key="in-ok"),
        _item(outgoing, state="awaiting_response", key="out-ok"),
        _item(expired, state="pending_reply", key="expired-key"),
        _item(dismissed, state="pending_reply", key="dismissed-key"),
        _item(snoozed, state="pending_reply", key="snoozed-key"),
        _item(completed, state="pending_reply", key="completed-key"),
        _item(promo, state="pending_reply", key="promo-key"),
        _item(webinar, state="pending_reply", key="webinar-key"),
        _item(info, state="pending_reply", key="info-key"),
    ]
    future = (NOW + timedelta(days=1)).isoformat()
    past = (NOW - timedelta(days=1)).isoformat()
    states = [
        {"recommendation_key": "dismissed-key", "status": "dismissed", "source_id": "dismissed"},
        {
            "recommendation_key": "snoozed-key",
            "status": "snoozed",
            "snoozed_until": future,
            "source_id": "snoozed",
        },
        {"recommendation_key": "completed-key", "status": "completed", "source_id": "completed"},
        {
            "recommendation_key": "expired-key",
            "status": "snoozed",
            "snoozed_until": past,
            "source_id": "expired",
        },
    ]
    return emails, active, states


def _review(**overrides):
    emails, active, states = _sample_mail()
    calls = {"sync": 0, "mode": 0}

    def load_emails(**_kwargs):
        return emails

    def detect(rows, *, account_email):
        assert account_email == ACCOUNT
        assert rows == emails
        return {"active": active}

    kwargs = dict(
        account_email=ACCOUNT,
        now=NOW,
        load_emails=load_emails,
        load_evening=lambda mode, account_email: None,
        load_states=lambda gmail_account_email: states if gmail_account_email == ACCOUNT else [],
        load_sync=lambda _account: {"updated_at": NOW.isoformat(), "backfill_status": "complete"},
        detect_followups=detect,
        load_checkpoint=lambda **_kwargs: None,
    )
    kwargs.update(overrides)
    with _enabled(), _kolkata(), patch("briefing_modes.run_mode_briefing") as mode, patch(
        "scheduler.run_gmail_sync_once"
    ) as sync:
        out = build_review(**kwargs)
        calls["mode"] = mode.call_count
        calls["sync"] = sync.call_count
    assert calls["mode"] == 0
    assert calls["sync"] == 0
    return out


def test_review_without_evening_wrapup():
    out = _review()
    assert out["wrapup_source"] == "stored_mail"
    assert "stored today" in out["summary_text"]
    assert out["metrics"]["incoming_awaiting_user"] == 2
    titles = {row["source_email_id"] for row in out["incoming_awaiting_user"]}
    assert titles == {"in-ok", "expired"}
    outgoing = {row["source_email_id"] for row in out["outgoing_awaiting_recipient"]}
    assert outgoing == {"out-ok"}
    unresolved = {row["source_email_id"] for row in out["unresolved_items"]}
    assert unresolved == {"open-item"}
    due = {row["due_date"] for row in out["deadlines"]}
    assert "2026-11-06" in due
    assert "2026-11-04" in due
    assert any(row["overdue"] for row in out["deadlines"] if row["due_date"] == "2026-11-04")
    freshness = out["data_freshness"]
    assert freshness["complete"] is True
    assert freshness["finish_allowed"] is True
    assert freshness["historical_backfill_complete"] is True
    assert "not fully processed" not in freshness["message"]
    assert "Historical mailbox backfill is complete." in freshness["message"]


def test_review_uses_todays_evening_wrapup_without_replacing_counts():
    def load_evening(mode, account_email):
        assert mode == "evening"
        assert account_email == ACCOUNT
        return {
            "period_key": "2026-11-05",
            "content": {
                "headline": "Evening wrap-up claims 99 emails.",
                "highlights": ["Filing still open."],
            },
        }

    out = _review(load_evening=load_evening)
    assert out["wrapup_source"] == "evening_wrapup"
    assert "Evening wrap-up claims 99 emails." in out["summary_text"]
    assert out["metrics"]["emails_received"] != 99
    assert "stored today" in out["verified_counts"]


def test_old_evening_wrapup_is_not_today():
    def load_evening(_mode, _account):
        return {
            "period_key": "2026-11-04",
            "content": {"headline": "Yesterday's wrap-up"},
        }

    out = _review(load_evening=load_evening)
    assert out["wrapup_source"] == "stored_mail"
    assert "Yesterday" not in out["summary_text"]


def test_finish_is_idempotent_and_does_not_complete_work():
    store: dict[tuple[str, str], dict] = {}
    completed: list = []

    def upsert(**kwargs):
        key = (kwargs["gmail_account_email"], kwargs["local_date"])
        row = dict(kwargs)
        row["id"] = store.get(key, {}).get("id", "ckpt-1")
        store[key] = row
        return row

    def load_checkpoint(*, gmail_account_email, local_date):
        return store.get((gmail_account_email, local_date))

    with _enabled(), _kolkata(), patch(
        "db.upsert_recommendation_state", side_effect=AssertionError("must not complete")
    ), patch("db.put_catchup_cache", return_value={}):
        first = finish_my_day(
            account_email=ACCOUNT,
            decisions=[{"action": "remind_tomorrow", "recommendation_key": "out-ok"}],
            now=NOW,
            review=_review(load_checkpoint=load_checkpoint),
            upsert_checkpoint=upsert,
        )
        later = NOW + timedelta(hours=1)
        second = finish_my_day(
            account_email=ACCOUNT,
            decisions=[{"action": "dismiss", "recommendation_key": "in-ok"}],
            now=later,
            review=_review(load_checkpoint=load_checkpoint),
            upsert_checkpoint=upsert,
        )
        other = finish_my_day(
            account_email=OTHER,
            now=NOW,
            review={
                "enabled": True,
                "local_date": "2026-11-05",
                "timezone": "Asia/Kolkata",
                "summary_text": "Other account",
                "wrapup_source": "stored_mail",
                "incoming_awaiting_user": [],
                "outgoing_awaiting_recipient": [],
                "deadlines": [],
                "unresolved_items": [{"source_email_id": "other-mail", "title": "Other"}],
                "data_freshness": {
                    "finish_allowed": True,
                    "historical_backfill_complete": True,
                    "message": "Today's mailbox window is covered.",
                },
            },
            upsert_checkpoint=upsert,
        )
    assert completed == []
    assert first["saved"] is True
    assert second["checkpoint"]["id"] == "ckpt-1"
    assert second["checkpoint"]["closed_at"] != first["checkpoint"]["closed_at"]
    assert len([key for key in store if key[0] == ACCOUNT]) == 1
    assert second["checkpoint"]["decisions"][0]["action"] == "dismiss"
    assert second["unresolved_count"] > 0
    assert other["checkpoint"]["gmail_account_email"] == OTHER
    assert load_checkpoint(gmail_account_email=ACCOUNT, local_date="2026-11-05")[
        "summary"
    ].startswith("1") or "stored today" in load_checkpoint(
        gmail_account_email=ACCOUNT, local_date="2026-11-05"
    )["summary"]
    assert load_checkpoint(gmail_account_email=ACCOUNT, local_date="2026-11-05")[
        "gmail_account_email"
    ] == ACCOUNT


def test_saved_review_does_not_scan_mail_again():
    clear_review_cache()
    with _enabled(), _kolkata(), patch("db.put_catchup_cache", return_value={}):
        local_date = today_window(NOW)["local_date"]
        store_review_cache(
            ACCOUNT,
            local_date,
            {
                "enabled": True,
                "local_date": local_date,
                "summary_text": "Already built",
                "metrics": {"emails_received": 3},
            },
        )

        def explode(**_kwargs):
            raise AssertionError("stored review should not scan mail")

        with patch("db.list_emails_for_briefing_range", side_effect=explode), patch(
            "db.get_catchup_cache", side_effect=explode
        ), patch("db.put_catchup_cache", side_effect=explode):
            out = build_review(account_email=ACCOUNT, now=NOW)
    assert out["cached"] is True
    assert out["summary_text"] == "Already built"
    assert read_review_cache(ACCOUNT, local_date)["summary_text"] == "Already built"
    clear_review_cache()


def test_promotional_examples_are_not_pending_replies():
    newsletter = _email(
        email_id="creator-bytes",
        sender="Team Creator <news@creator.example>",
        subject="Creator Bytes: Partner chats, customer stories, and more!",
        action_required=True,
        importance="high",
        category="work",
        summary="Digest of partner chats and customer stories.",
    )
    course = _email(
        email_id="valuation-course",
        sender="Program Coordinator <courses@example.edu>",
        subject="Admission Open - 50 Hrs. Online Educational Course on Valuation {87th Online Batch}",
        action_required=True,
        importance="high",
        category="work",
        summary="Please confirm your seat for the 87th online batch.",
    )
    client = _email(
        email_id="client-request",
        sender="Client <client@firm.com>",
        subject="Please review the August monitoring report",
        action_required=True,
        summary="Please review the report and confirm the figures.",
    )
    info = _email(
        email_id="case-law",
        sender="Taxmann <alert@newsletter.taxmann.com>",
        subject="TDS credit allowable in the year of interest income",
        action_required=False,
        importance="high",
        category="updates",
        summary="Case law update. No reply needed.",
    )
    ambiguous = _email(
        email_id="ambiguous",
        sender="Alex <alex@client.com>",
        subject="Following up on the file",
        action_required=True,
        summary="There may be something to discuss.",
    )
    answered = _email(
        email_id="answered",
        sender="Client <client@firm.com>",
        subject="Please review the invoice total",
        action_required=True,
        summary="Please review the invoice total.",
    )
    summit = _email(
        email_id="summit",
        sender="Events <events@vc.example>",
        subject="Real Asset Investment Summit 2026 | 26th November | The Leela Mumbai",
        action_required=True,
        importance="high",
        summary="Kindly reply to reserve a delegate seat.",
    )
    emails = [newsletter, course, client, info, ambiguous, answered, summit]
    active = [
        _item(newsletter, state="pending_reply", key="news"),
        _item(course, state="pending_reply", key="course"),
        _item(client, state="pending_reply", key="client"),
        _item(info, state="pending_reply", key="info"),
        _item(ambiguous, state="pending_reply", key="amb"),
        _item(answered, state="pending_reply", key="answered"),
        _item(summit, state="pending_reply", key="summit"),
    ]

    def load_threads(*, gmail_account_email, thread_id):
        assert gmail_account_email == ACCOUNT
        if thread_id != answered["gmail_thread_id"]:
            return [next(email for email in emails if email["gmail_thread_id"] == thread_id)]
        return [
            answered,
            _email(
                email_id="answered-reply",
                sender=ACCOUNT,
                subject="Re: Please review the invoice total",
                labels=["SENT"],
                summary="Confirmed.",
            )
            | {"gmail_thread_id": answered["gmail_thread_id"], "internal_date": 50},
        ]

    out = _review(
        load_emails=lambda **_kwargs: emails,
        detect_followups=lambda rows, *, account_email: {"active": active},
        load_threads=load_threads,
    )
    incoming = {row["source_email_id"] for row in out["incoming_awaiting_user"]}
    review_ids = {row["source_email_id"] for row in out["needs_review"]}
    unresolved = {row["source_email_id"] for row in out["unresolved_items"]}
    assert incoming == {"client-request"}
    assert "creator-bytes" not in incoming | review_ids | unresolved
    assert "valuation-course" not in incoming | review_ids | unresolved
    assert "summit" not in incoming | review_ids | unresolved
    assert "case-law" not in incoming | review_ids | unresolved
    assert "answered" not in incoming | review_ids | unresolved
    assert review_ids == {"ambiguous"}
    assert all(row.get("review_status") == "needs_review" for row in out["needs_review"])
    assert classify_reply_need(newsletter, newsletter["email_analyses"][0]) == "not_a_request"
    assert classify_reply_need(course, course["email_analyses"][0]) == "not_a_request"
    assert classify_reply_need(client, client["email_analyses"][0]) == "confirmed_request"
    assert classify_reply_need(ambiguous, ambiguous["email_analyses"][0]) == "needs_review"


def test_single_broadcast_phrase_is_needs_review_not_confirmed():
    email = _email(
        email_id="one-phrase",
        sender="Maya <maya@client.com>",
        subject="Newsletter draft for Friday",
        action_required=True,
        summary="Sharing a note.",
    )
    edition = _email(
        email_id="edition",
        sender="Zoho <news@zoho.example>",
        subject="Zoho Projects Partner Newsletter - Beyond Task Lists - Edition#1",
        action_required=True,
        summary="Partner updates.",
    )
    assert classify_reply_need(email, email["email_analyses"][0]) == "needs_review"
    assert classify_reply_need(edition, edition["email_analyses"][0]) == "not_a_request"


def test_stale_sync_is_separate_from_historical_backfill():
    stale_at = (NOW - timedelta(hours=2)).isoformat()
    fresh = _sync_freshness(
        {"updated_at": NOW.isoformat(), "backfill_status": "running"},
        [_email(email_id="kept", sender="A <a@co.com>", subject="Please review this")],
        now=NOW,
    )
    assert fresh["incremental_fresh"] is True
    assert fresh["historical_backfill_complete"] is False
    assert fresh["window_covered"] is True
    assert fresh["finish_allowed"] is True
    assert fresh["complete"] is False
    assert "not fully synchronized" in fresh["message"]
    assert "fully synchronized." not in fresh["message"].replace("not fully synchronized", "")
    stale = _sync_freshness(
        {"updated_at": stale_at, "backfill_status": "running"},
        [_email(email_id="kept", sender="A <a@co.com>", subject="Please review this")],
        now=NOW,
    )
    assert stale["stale"] is True
    assert stale["incremental_fresh"] is False
    assert stale["finish_allowed"] is False
    assert "sync interval" in stale["message"]
    behind = _sync_freshness(
        {"updated_at": NOW.isoformat(), "backfill_status": "complete"},
        [
            {
                **_email(email_id="newer", sender="A <a@co.com>", subject="Please review this"),
                "internal_date": int((NOW + timedelta(minutes=30)).timestamp() * 1000),
            }
        ],
        now=NOW,
    )
    assert behind["checkpoint_behind_stored_mail"] is True
    assert behind["window_covered"] is False
    assert behind["finish_allowed"] is False
    assert "newer than the last sync checkpoint" in behind["message"]


def test_finish_rejects_unverified_snapshot_without_saving():
    calls = {"upsert": 0}

    def upsert(**_kwargs):
        calls["upsert"] += 1
        return {"id": "nope"}

    review = _review()
    review["data_freshness"] = {
        "finish_allowed": False,
        "finish_block_reason": "Stored mail is newer than the last sync checkpoint.",
        "message": "Stored mail is newer than the last sync checkpoint.",
    }
    with _enabled(), patch("db.put_catchup_cache", return_value={}):
        out = finish_my_day(
            account_email=ACCOUNT,
            now=NOW,
            review=review,
            upsert_checkpoint=upsert,
        )
    assert out["saved"] is False
    assert out["ok"] is False
    assert calls["upsert"] == 0
    assert "checkpoint" in out["message"] or "newer" in out["message"]


def test_refresh_uses_existing_sync_and_waits_for_analysis():
    sync_calls = []
    analyze_calls = []
    pending = {"left": 2}

    def run_sync():
        sync_calls.append("sync")
        return {"status": "started", "saved": 1}

    def count_pending(*, gmail_account_email, start_ms):
        assert gmail_account_email == ACCOUNT
        assert start_ms
        left = pending["left"]
        pending["left"] = 0
        return left

    def analyze_pending(*, gmail_account_email, limit, statuses):
        assert gmail_account_email == ACCOUNT
        assert limit == 30
        assert "pending" in statuses
        analyze_calls.append(gmail_account_email)
        return {"analyzed": 2}

    canned = {
        "enabled": True,
        "data_freshness": {
            "finish_allowed": True,
            "historical_backfill_complete": True,
            "message": "Today's mailbox window is covered. Historical mailbox backfill is complete.",
        },
    }
    with _enabled(), _kolkata(), patch("end_my_day_service.build_review", return_value=canned):
        out = refresh_mailbox(
            account_email=ACCOUNT,
            now=NOW,
            run_sync=run_sync,
            analyze_pending=analyze_pending,
            count_pending=count_pending,
            syncing=lambda: False,
        )
    assert sync_calls == ["sync"]
    assert analyze_calls == [ACCOUNT]
    assert out["status"] == "success"
    assert out["ok"] is True
    assert out["review"]["data_freshness"]["finish_allowed"] is True


def test_failed_refresh_does_not_report_success():
    def explode(*_args, **_kwargs):
        raise AssertionError("failed refresh must not rebuild the review")

    with _enabled(), patch("end_my_day_service.build_review", side_effect=explode):
        failed = refresh_mailbox(
            account_email=ACCOUNT,
            now=NOW,
            run_sync=lambda: {"status": "auth_unavailable", "error": "Gmail authentication unavailable."},
            analyze_pending=lambda **_kwargs: {},
            count_pending=lambda **_kwargs: 0,
            syncing=lambda: False,
        )
        busy = refresh_mailbox(
            account_email=ACCOUNT,
            now=NOW,
            run_sync=lambda: (_ for _ in ()).throw(AssertionError("must not sync twice")),
            syncing=lambda: True,
        )
    assert failed["ok"] is False
    assert failed["status"] == "failed"
    assert failed["review"] is None
    assert "success" not in failed["message"].lower()
    assert busy["status"] == "already_running"
    assert busy["ok"] is False


def test_refresh_keeps_accounts_separate_and_does_not_insert_mail():
    seen = []
    pending = {"left": 1}

    def count_pending(*, gmail_account_email, start_ms):
        seen.append(gmail_account_email)
        assert start_ms
        left = pending["left"]
        pending["left"] = 0
        return left

    canned = {
        "enabled": True,
        "data_freshness": {
            "finish_allowed": False,
            "historical_backfill_complete": False,
            "message": "Historical mailbox backfill is still incomplete, so older mail may be missing. The mailbox is not fully synchronized.",
        },
    }
    with _enabled(), _kolkata(), patch(
        "end_my_day_service.build_review", return_value=canned
    ), patch("db.upsert_email_record", side_effect=AssertionError("refresh must not insert mail")):
        out = refresh_mailbox(
            account_email=OTHER,
            now=NOW,
            run_sync=lambda: {"status": "started"},
            analyze_pending=lambda **kwargs: seen.append(kwargs["gmail_account_email"]) or {},
            count_pending=count_pending,
            syncing=lambda: False,
        )
    assert seen == [OTHER, OTHER, OTHER]
    assert ACCOUNT not in seen
    assert out["status"] == "partial"
    assert "not fully synchronized" in out["message"]


if __name__ == "__main__":
    test_flag_default_off()
    test_disabled_does_not_load_mail()
    test_timezone_boundary_uses_local_date()
    test_review_without_evening_wrapup()
    test_review_uses_todays_evening_wrapup_without_replacing_counts()
    test_old_evening_wrapup_is_not_today()
    test_finish_is_idempotent_and_does_not_complete_work()
    test_saved_review_does_not_scan_mail_again()
    test_promotional_examples_are_not_pending_replies()
    test_single_broadcast_phrase_is_needs_review_not_confirmed()
    test_stale_sync_is_separate_from_historical_backfill()
    test_finish_rejects_unverified_snapshot_without_saving()
    test_refresh_uses_existing_sync_and_waits_for_analysis()
    test_failed_refresh_does_not_report_success()
    test_refresh_keeps_accounts_separate_and_does_not_insert_mail()
    print("All End My Day tests passed.")
