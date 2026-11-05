"""
Safe end-to-end verification for Tasks 5 & 6.

- Does not send email, WhatsApp, or mutate Gmail.
- Does not execute migrations or enable scheduler jobs.
- Uses isolated sample data for VIP/filter/ranking/dedupe.
- Live HTTP checks use the running backend; DB probes are read-only except
  optional isolated preference/feedback rows keyed under a TEST account
  (never the production ALLOWED_GMAIL_ACCOUNT) when tables exist.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
load_dotenv(ROOT / ".env", override=True)
load_dotenv(BASE / ".env", override=False)

BACKEND = (os.getenv("BACKEND_URL") or "http://localhost:8008").rstrip("/")
FRONTEND = (os.getenv("FRONTEND_URL") or "http://localhost:3003").rstrip("/")
TEST_ACCOUNT = "tasks56.verify@example.invalid"
PROD_ACCOUNT = (os.getenv("ALLOWED_GMAIL_ACCOUNT") or "").strip().lower()

RESULTS: list[dict[str, str]] = []


def record(feature: str, status: str, evidence: str, issue: str = "") -> None:
    RESULTS.append(
        {
            "feature": feature,
            "status": status,
            "evidence": evidence[:500],
            "issue": issue[:400],
        }
    )
    print(f"[{status}] {feature}: {evidence[:200]}")
    if issue:
        print(f"         issue: {issue[:200]}")


def http_json(method: str, path: str, body: dict | None = None, cookie: str | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BACKEND}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:300]}
        return exc.code, payload, dict(exc.headers)


def main() -> int:
    # ------------------------------------------------------------------ #
    # 1–2 OpenAPI / schema inspection
    # ------------------------------------------------------------------ #
    try:
        status, openapi, _ = http_json("GET", "/openapi.json")
        assert status == 200
        paths = openapi.get("paths") or {}
        schemas = (openapi.get("components") or {}).get("schemas") or {}
        required = [
            "/db/briefings/preferences",
            "/db/briefings/recommendations/{recommendation_id}/feedback",
            "/db/briefings/feedback/summary",
            "/db/briefings/latest",
            "/db/briefings/history",
        ]
        missing = [p for p in required if p not in paths]
        if missing:
            record(
                "API schema inspection",
                "FAIL",
                f"openapi ok but missing paths: {missing}",
            )
        else:
            pref_schema = schemas.get("BriefingPreferencesPatchRequest", {})
            fb_schema = schemas.get("RecommendationFeedbackRequest", {})
            record(
                "API schema inspection",
                "PASS",
                (
                    f"OpenAPI exposes preferences/feedback/latest/history. "
                    f"Patch props={list((pref_schema.get('properties') or {}).keys())}; "
                    f"feedback enum={((fb_schema.get('properties') or {}).get('feedback_type') or {}).get('enum')}"
                ),
            )
            print(
                "\n--- Swagger endpoints ---\n"
                "GET  /db/briefings/preferences\n"
                "PATCH /db/briefings/preferences\n"
                "  body example:\n"
                + json.dumps(
                    {
                        "timezone": "Asia/Kolkata",
                        "working_days": [1, 2, 3, 4, 5],
                        "working_hours": {"start": "09:00", "end": "18:00"},
                        "vip_contacts": ["vip.client@example.com"],
                        "filtering_preferences": {
                            "hide_newsletters": True,
                            "hide_promotional": True,
                            "hide_automated": False,
                            "hide_low_priority_informational": False,
                        },
                        "briefing_sections": {
                            "important_emails": True,
                            "pending_replies": True,
                            "follow_up_recommendations": True,
                            "deadlines": True,
                            "meetings": True,
                            "top_three_priorities": True,
                            "informational_updates": True,
                            "low_priority_emails": False,
                        },
                        "mode_schedules": {
                            "morning": {"enabled": False, "local_time": "08:00"},
                            "evening": {
                                "enabled": False,
                                "local_time": "19:00",
                                "timezone": "Asia/Kolkata",
                            },
                            "weekly": {
                                "enabled": False,
                                "local_time": "17:00",
                                "weekday": "sat",
                            },
                            "monday": {
                                "enabled": False,
                                "local_time": "08:30",
                                "weekday": "mon",
                            },
                        },
                    },
                    indent=2,
                )
                + "\nPOST /db/briefings/recommendations/{recommendation_id}/feedback\n"
                '  body: {"feedback_type":"useful"}\n'
                "GET  /db/briefings/feedback/summary\n"
            )
    except Exception as exc:
        record("API schema inspection", "FAIL", "Could not load openapi.json", str(exc))

    # ------------------------------------------------------------------ #
    # 3–4 Migrations + flags
    # ------------------------------------------------------------------ #
    from supabase import create_client

    client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    table_status: dict[str, str] = {}
    for table in (
        "briefing_preferences",
        "briefing_recommendation_feedback",
        "briefing_schedule_claims",
        "briefing_quality_counters",
        "briefing_recommendation_state",
        "daily_briefings",
        "smart_briefing_mode_runs",
    ):
        try:
            client.table(table).select("*").limit(1).execute()
            table_status[table] = "EXISTS"
        except Exception as exc:
            msg = str(exc)
            table_status[table] = (
                "MISSING"
                if "PGRST205" in msg or "Could not find" in msg or "does not exist" in msg.lower()
                else f"ERROR:{msg[:80]}"
            )

    if table_status["briefing_preferences"] == "EXISTS" and table_status[
        "briefing_recommendation_feedback"
    ] == "EXISTS":
        record(
            "Supabase migrations (Task 5/6 tables)",
            "PASS",
            f"Verified: { {k:v for k,v in table_status.items() if k.startswith('briefing_')} }",
            issue=(
                "briefing_recommendation_state MISSING (Task 4) — feedback ownership "
                "fallback limited; interactive recommendation status blocked"
                if table_status.get("briefing_recommendation_state") == "MISSING"
                else ""
            ),
        )
    else:
        record(
            "Supabase migrations (Task 5/6 tables)",
            "BLOCKED",
            str(table_status),
            "Apply docs/migrations/2026-11-03_briefing_preferences.sql and "
            "2026-11-03_briefing_feedback_and_claims.sql",
        )

    # Process-local flags (dotenv). Running uvicorn may differ until restart.
    from briefing_preferences import personalization_enabled
    from briefing_feedback import adaptive_ranking_enabled, feedback_enabled

    flag_vals = {
        "BRIEFING_PERSONALIZATION_ENABLED": os.getenv("BRIEFING_PERSONALIZATION_ENABLED"),
        "BRIEFING_FEEDBACK_ENABLED": os.getenv("BRIEFING_FEEDBACK_ENABLED"),
        "BRIEFING_ADAPTIVE_RANKING_ENABLED": os.getenv("BRIEFING_ADAPTIVE_RANKING_ENABLED"),
        "runtime_personalization_enabled()": personalization_enabled(),
        "runtime_feedback_enabled()": feedback_enabled(),
        "runtime_adaptive_enabled()": adaptive_ranking_enabled(),
    }
    if all(
        [
            personalization_enabled(),
            feedback_enabled(),
            adaptive_ranking_enabled(),
        ]
    ):
        record(
            "Environment flags (process dotenv)",
            "PASS",
            f"All Task 5/6 flags true via dotenv: {flag_vals}",
            issue=(
                "Note: .env sets flags false earlier then true later; last wins for "
                "dotenv. Running backend must be restarted to pick up changes."
            ),
        )
    else:
        record(
            "Environment flags (process dotenv)",
            "FAIL",
            str(flag_vals),
            "Expected Task 5/6 flags enabled for live verification (user .env ends with true).",
        )

    # Live backend flag behavior via unauthenticated probe shape
    st, pref_body, _ = http_json("GET", "/db/briefings/preferences")
    if st == 401:
        record(
            "Live backend auth gate (preferences)",
            "PASS",
            "GET /db/briefings/preferences → 401 without session (expected).",
        )
    else:
        record(
            "Live backend auth gate (preferences)",
            "FAIL",
            f"Expected 401, got {st}: {pref_body}",
        )

    # ------------------------------------------------------------------ #
    # 5 Preferences GET/PATCH with isolated TEST account (DB), not prod
    # ------------------------------------------------------------------ #
    if table_status["briefing_preferences"] != "EXISTS":
        record(
            "Preferences GET/PATCH (isolated DB)",
            "BLOCKED",
            "briefing_preferences missing",
        )
    elif TEST_ACCOUNT == PROD_ACCOUNT:
        record(
            "Preferences GET/PATCH (isolated DB)",
            "BLOCKED",
            "Refusing write: TEST_ACCOUNT equals production account",
        )
    else:
        try:
            from briefing_preferences import (
                get_preferences_for_account,
                save_preferences_for_account,
            )

            before = get_preferences_for_account(TEST_ACCOUNT)
            saved = save_preferences_for_account(
                TEST_ACCOUNT,
                {
                    "timezone": "Asia/Kolkata",
                    "vip_contacts": ["vip.client@example.com"],
                    "filtering_preferences": {
                        "hide_newsletters": True,
                        "hide_promotional": True,
                    },
                    "mode_schedules": {
                        "evening": {
                            "enabled": False,
                            "local_time": "19:30",
                            "timezone": "Asia/Kolkata",
                        }
                    },
                },
            )
            again = get_preferences_for_account(TEST_ACCOUNT)
            vip = again["preferences"].get("vip_contacts") or []
            ok = (
                again["source"] == "stored"
                and "vip.client@example.com" in vip
                and saved["preferences"]["timezone"] == "Asia/Kolkata"
            )
            # cleanup isolated row
            client.table("briefing_preferences").delete().eq(
                "gmail_account_email", TEST_ACCOUNT
            ).execute()
            record(
                "Preferences GET/PATCH (isolated DB)",
                "PASS" if ok else "FAIL",
                f"before_source={before.get('source')} saved_vip={vip} "
                f"evening_enabled={again['preferences']['mode_schedules']['evening']['enabled']}",
                "" if ok else "Stored preferences did not round-trip",
            )
        except Exception as exc:
            record(
                "Preferences GET/PATCH (isolated DB)",
                "FAIL",
                "Exception during isolated preference write/read",
                str(exc),
            )

    # Live HTTP preferences still need session — report without mutating prod
    st, body, _ = http_json("GET", "/db/briefings/preferences")
    if st == 401:
        record(
            "Preferences GET/PATCH (live HTTP session)",
            "BLOCKED",
            "No authenticated browser session cookie available to this verifier. "
            "Swagger UI while logged in required for live HTTP PATCH.",
            "Open http://localhost:8008/docs after Gmail OAuth and try GET/PATCH manually.",
        )
    elif st == 200:
        record(
            "Preferences GET (live HTTP)",
            "PASS",
            f"enabled={body.get('enabled')} source={body.get('source')} "
            f"keys={list((body.get('preferences') or {}).keys())}",
        )
    else:
        record(
            "Preferences GET (live HTTP)",
            "FAIL",
            f"status={st} body={body}",
        )

    # ------------------------------------------------------------------ #
    # 6–7 VIP + filtering (isolated sample emails, no DB email writes)
    # ------------------------------------------------------------------ #
    try:
        from briefing_preferences import (
            default_preferences,
            filter_emails_for_preferences,
            should_filter_email,
        )
        from briefing_priority import score_email_priority

        day = date(2026, 11, 3)
        vip_email = {
            "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "sender": "VIP <vip.client@example.com>",
            "from_email": "vip.client@example.com",
            "subject": "Contract review",
            "email_analyses": [
                {
                    "importance": "medium",
                    "action_required": False,
                    "confidence": 0.5,
                    "summary": "Please review",
                }
            ],
        }
        newsletter = {
            "id": "11111111-2222-4333-8444-555555555555",
            "sender": "News <news@newsletter.example>",
            "subject": "Weekly Newsletter",
            "label_ids": ["CATEGORY_PROMOTIONS"],
            "email_analyses": [{"importance": "low", "action_required": False}],
        }
        urgent_promo = {
            "id": "99999999-aaaa-4bbb-8ccc-dddddddddddd",
            "sender": "Billing <billing@acme.com>",
            "subject": "Urgent invoice",
            "label_ids": ["CATEGORY_PROMOTIONS"],
            "email_analyses": [
                {
                    "importance": "high",
                    "action_required": True,
                    "deadlines": ["due today"],
                }
            ],
        }
        base = score_email_priority(vip_email, briefing_day=day)
        boosted = score_email_priority(
            vip_email, briefing_day=day, vip_emails={"vip.client@example.com"}
        )
        if boosted["score"] > base["score"] and boosted["breakdown"].get("vip", 0) > 0:
            record(
                "VIP prioritization",
                "PASS",
                f"base={base['score']} vip={boosted['score']} "
                f"vip_bonus={boosted['breakdown'].get('vip')}",
            )
        else:
            record(
                "VIP prioritization",
                "FAIL",
                f"base={base} boosted={boosted}",
                "VIP bonus not applied",
            )

        prefs = default_preferences()
        prefs["filtering_preferences"]["hide_newsletters"] = True
        prefs["filtering_preferences"]["hide_promotional"] = True
        os.environ["BRIEFING_PERSONALIZATION_ENABLED"] = "true"
        filtered = filter_emails_for_preferences(
            [newsletter, urgent_promo, vip_email], prefs
        )
        ids = {str(e.get("id")) for e in filtered}
        if (
            should_filter_email(newsletter, prefs)
            and not should_filter_email(urgent_promo, prefs)
            and str(urgent_promo["id"]) in ids
            and str(newsletter["id"]) not in ids
        ):
            record(
                "Newsletter filtering + urgent override",
                "PASS",
                f"kept_ids={sorted(ids)}; newsletter filtered, urgent retained",
            )
        else:
            record(
                "Newsletter filtering + urgent override",
                "FAIL",
                f"kept={ids}",
                "Filter/override behavior incorrect",
            )
    except Exception as exc:
        record("VIP prioritization", "FAIL", "exception", str(exc))
        record("Newsletter filtering + urgent override", "FAIL", "exception", str(exc))

    # ------------------------------------------------------------------ #
    # 8–10 Feedback, adaptive ranking, duplicate prevention
    # ------------------------------------------------------------------ #
    if table_status["briefing_recommendation_feedback"] != "EXISTS":
        record("Recommendation feedback persistence", "BLOCKED", "feedback table missing")
    else:
        try:
            os.environ["BRIEFING_FEEDBACK_ENABLED"] = "true"
            from briefing_feedback import (
                feedback_summary_for_account,
                submit_recommendation_feedback,
            )

            # Seed recommendation_state substitute: ownership via state table OR owned rec.
            # Task 4 state table missing → submit requires state row OR owned recommendation.
            # Insert feedback ownership via temporary state? Table missing.
            # Use get_recommendation_state path — without state table, BLOCKED for live persist
            # unless we can upsert feedback only after ownership check.
            # Ownership check: get_owned_recommendation needs interactive briefing + latest briefing
            # OR get_recommendation_state.
            if table_status.get("briefing_recommendation_state") == "MISSING":
                # Direct DB upsert of feedback for TEST account to prove table write path,
                # then exercise summary; API ownership still blocked without Task 4 table.
                from db import upsert_recommendation_feedback

                row = upsert_recommendation_feedback(
                    gmail_account_email=TEST_ACCOUNT,
                    recommendation_key="verify-tasks56-key",
                    feedback_type="useful",
                    metadata={"title": "isolated-verify"},
                )
                # update same key
                row2 = upsert_recommendation_feedback(
                    gmail_account_email=TEST_ACCOUNT,
                    recommendation_key="verify-tasks56-key",
                    feedback_type="not_useful",
                    metadata={"title": "isolated-verify"},
                )
                summary = None
                try:
                    # summary lists feedback for account — works without state table
                    from db import list_recommendation_feedback_for_account

                    rows = list_recommendation_feedback_for_account(
                        gmail_account_email=TEST_ACCOUNT
                    )
                    summary = [r.get("feedback_type") for r in rows]
                finally:
                    client.table("briefing_recommendation_feedback").delete().eq(
                        "gmail_account_email", TEST_ACCOUNT
                    ).execute()
                ok = row.get("feedback_type") == "useful" and row2.get(
                    "feedback_type"
                ) == "not_useful"
                record(
                    "Recommendation feedback persistence",
                    "PASS" if ok else "FAIL",
                    f"upsert useful→not_useful; listed={summary}",
                    issue=(
                        "API submit_recommendation_feedback ownership check depends on "
                        "missing briefing_recommendation_state (Task 4) or interactive "
                        "recommendation resolution — HTTP feedback endpoint BLOCKED until "
                        "Task 4 migration applied / INTERACTIVE_BRIEFING_ENABLED with briefing."
                    ),
                )
            else:
                # create state row for ownership then feedback
                from db import upsert_recommendation_state

                upsert_recommendation_state(
                    gmail_account_email=TEST_ACCOUNT,
                    source_type="briefing_item",
                    source_id="verify",
                    recommendation_key="verify-tasks56-key",
                    status="pending",
                    title="isolated",
                )
                os.environ["BRIEFING_FEEDBACK_ENABLED"] = "true"
                out = submit_recommendation_feedback(
                    account_email=TEST_ACCOUNT,
                    recommendation_key="verify-tasks56-key",
                    feedback_type="useful",
                )
                client.table("briefing_recommendation_feedback").delete().eq(
                    "gmail_account_email", TEST_ACCOUNT
                ).execute()
                client.table("briefing_recommendation_state").delete().eq(
                    "gmail_account_email", TEST_ACCOUNT
                ).execute()
                record(
                    "Recommendation feedback persistence",
                    "PASS",
                    f"submit ok: {out}",
                )
        except Exception as exc:
            record(
                "Recommendation feedback persistence",
                "FAIL",
                "exception during feedback write",
                str(exc),
            )

    try:
        from briefing_feedback import apply_feedback_adjustment, feedback_delta_for_email
        from briefing_feedback import aggregate_feedback_signals
        from briefing_priority import score_email_priority

        os.environ["BRIEFING_ADAPTIVE_RANKING_ENABLED"] = "true"
        signals = aggregate_feedback_signals(
            [
                {
                    "recommendation_key": "k1",
                    "feedback_type": "irrelevant",
                    "metadata": {"sender_email": "n@newsletter.com"},
                }
            ]
        )
        delta = feedback_delta_for_email(
            signals=signals, sender_email="x@newsletter.com"
        )
        email = {
            "sender": "n@newsletter.com",
            "from_email": "n@newsletter.com",
            "subject": "Deal",
            "email_analyses": [
                {"importance": "medium", "action_required": True, "confidence": 0.9}
            ],
        }
        base = score_email_priority(email, briefing_day=date(2026, 11, 3))
        adj = score_email_priority(
            email, briefing_day=date(2026, 11, 3), feedback_delta=delta
        )
        urgent_floor, _ = apply_feedback_adjustment(55, feedback_delta=-100, urgent=True)
        if delta < 0 and adj["score"] <= base["score"] and urgent_floor >= 40:
            record(
                "Adaptive ranking adjustments",
                "PASS",
                f"domain_delta={delta} base={base['score']} adj={adj['score']} "
                f"urgent_floor={urgent_floor}",
            )
        else:
            record(
                "Adaptive ranking adjustments",
                "FAIL",
                f"delta={delta} base={base} adj={adj} floor={urgent_floor}",
            )
    except Exception as exc:
        record("Adaptive ranking adjustments", "FAIL", "exception", str(exc))

    try:
        from briefing_feedback import filter_validated_recommendations

        cand = {
            "title": "Reply Acme",
            "source_email_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "recommendation_key": "dup-key",
            "priority_score": 50,
        }
        # Avoid writing quality counters for prod — patch bump via env TEST account
        with_counter_account = TEST_ACCOUNT
        kept, stats = filter_validated_recommendations(
            [cand, dict(cand)],
            account_email=with_counter_account,
            known_email_ids={"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        )
        # cleanup quality counters for test account if written
        try:
            client.table("briefing_quality_counters").delete().eq(
                "gmail_account_email", TEST_ACCOUNT
            ).execute()
        except Exception:
            pass
        if len(kept) == 1 and stats.get("duplicates_prevented") == 1:
            record(
                "Duplicate recommendation prevention",
                "PASS",
                f"kept={len(kept)} stats={stats}",
            )
        else:
            record(
                "Duplicate recommendation prevention",
                "FAIL",
                f"kept={kept} stats={stats}",
            )
    except Exception as exc:
        record("Duplicate recommendation prevention", "FAIL", "exception", str(exc))

    # ------------------------------------------------------------------ #
    # 11 Personalized scheduling without triggering jobs
    # ------------------------------------------------------------------ #
    try:
        from unittest.mock import MagicMock, patch
        from scheduler import (
            _register_personalized_briefing_jobs,
            _register_smart_briefing_mode_jobs,
        )

        fake = MagicMock()
        with patch.dict(
            os.environ,
            {
                "BRIEFING_PERSONALIZATION_ENABLED": "false",
                "SMART_BRIEFING_MODES_ENABLED": "false",
                "EVENING_BRIEFING_ENABLED": "false",
                "WEEKLY_DIGEST_ENABLED": "false",
                "MONDAY_PLANNING_ENABLED": "false",
            },
        ):
            _register_personalized_briefing_jobs(fake)
            _register_smart_briefing_mode_jobs(fake)
        if fake.add_job.call_count == 0:
            record(
                "Personalized scheduling (no duplicate jobs when disabled)",
                "PASS",
                "With personalization+modes flags false, no jobs registered "
                f"(add_job calls={fake.add_job.call_count}). "
                "Did not start scheduler or fire jobs.",
            )
        else:
            record(
                "Personalized scheduling (no duplicate jobs when disabled)",
                "FAIL",
                f"Unexpected add_job calls={fake.add_job.call_count}",
            )

        # Inspect that daily job id remains distinct in source
        import inspect
        import scheduler as sched_mod

        src = inspect.getsource(sched_mod.start_scheduler)
        if 'id="daily_email_briefing"' in src and "_register_personalized_briefing_jobs" in src:
            record(
                "Personalized scheduling architecture (daily job preserved)",
                "PASS",
                "start_scheduler still registers daily_email_briefing and "
                "separately calls _register_personalized_briefing_jobs",
            )
        else:
            record(
                "Personalized scheduling architecture (daily job preserved)",
                "FAIL",
                "Could not confirm daily_email_briefing registration in start_scheduler",
            )
    except Exception as exc:
        record(
            "Personalized scheduling (no duplicate jobs when disabled)",
            "FAIL",
            "exception",
            str(exc),
        )

    # Claim table exists — dry-run claim for TEST account then delete
    if table_status["briefing_schedule_claims"] == "EXISTS":
        try:
            from db import try_claim_briefing_schedule

            first = try_claim_briefing_schedule(
                gmail_account_email=TEST_ACCOUNT,
                job_kind="evening",
                period_key="verify-2026-11-03",
                metadata={"source": "e2e_verify"},
            )
            second = try_claim_briefing_schedule(
                gmail_account_email=TEST_ACCOUNT,
                job_kind="evening",
                period_key="verify-2026-11-03",
                metadata={"source": "e2e_verify"},
            )
            client.table("briefing_schedule_claims").delete().eq(
                "gmail_account_email", TEST_ACCOUNT
            ).execute()
            if first is not None and second is None:
                record(
                    "Schedule claim deduplication",
                    "PASS",
                    "First claim inserted, second returned None for same period_key",
                )
            else:
                record(
                    "Schedule claim deduplication",
                    "FAIL",
                    f"first={first} second={second}",
                )
        except Exception as exc:
            record("Schedule claim deduplication", "FAIL", "exception", str(exc))
    else:
        record("Schedule claim deduplication", "BLOCKED", "claims table missing")

    # ------------------------------------------------------------------ #
    # 12–13 Existing briefing APIs (read-only)
    # ------------------------------------------------------------------ #
    st, latest, _ = http_json("GET", "/db/briefings/latest")
    if st == 401:
        record(
            "Existing Daily Briefing generation/API",
            "BLOCKED",
            "GET /db/briefings/latest requires authenticated session (401). "
            "Did not call /internal/briefing-now (would use OpenAI + may notify WhatsApp).",
        )
    elif st == 200 and "briefing" in latest:
        record(
            "Existing Daily Briefing generation/API",
            "PASS",
            f"status={latest.get('status')} has_briefing={latest.get('briefing') is not None}",
        )
    else:
        record(
            "Existing Daily Briefing generation/API",
            "FAIL",
            f"status={st} body_keys={list(latest) if isinstance(latest, dict) else latest}",
        )

    # Read-only confirm a daily_briefings row exists in DB (no regenerate)
    try:
        rows = (
            client.table("daily_briefings")
            .select("id,briefing_date,gmail_account_email,created_at")
            .order("created_at", desc=True)
            .limit(3)
            .execute()
        )
        data = rows.data or []
        if data:
            # redact account partially
            safe = [
                {
                    "id": r.get("id"),
                    "briefing_date": r.get("briefing_date"),
                    "created_at": r.get("created_at"),
                }
                for r in data
            ]
            record(
                "Existing briefing history (DB read-only)",
                "PASS",
                f"Found {len(data)} recent daily_briefings rows: {safe}",
            )
        else:
            record(
                "Existing briefing history (DB read-only)",
                "FAIL",
                "daily_briefings table empty",
            )
    except Exception as exc:
        record("Existing briefing history (DB read-only)", "FAIL", "exception", str(exc))

    st, hist, _ = http_json("GET", "/db/briefings/history")
    if st == 401:
        record(
            "Existing briefing history API",
            "BLOCKED",
            "GET /db/briefings/history → 401 without session",
        )
    elif st == 200:
        record(
            "Existing briefing history API",
            "PASS",
            f"keys={list(hist.keys()) if isinstance(hist, dict) else type(hist)}",
        )
    else:
        record("Existing briefing history API", "FAIL", f"status={st}")

    # ------------------------------------------------------------------ #
    # 14 Frontend layout unchanged — static source check + frontend up
    # ------------------------------------------------------------------ #
    try:
        st_f = urllib.request.urlopen(FRONTEND, timeout=10).status
    except Exception as exc:
        st_f = None
        record("Frontend availability", "FAIL", "frontend not reachable", str(exc))
    else:
        record("Frontend availability", "PASS", f"GET {FRONTEND} → {st_f}")

    body_path = ROOT / "frontend" / "src" / "home" / "DailyBriefingBody.tsx"
    panel_path = ROOT / "frontend" / "src" / "BriefingPanel.tsx"
    if body_path.exists():
        text = body_path.read_text(encoding="utf-8")
        # Ensure no Task 5/6 UI controls were added
        banned = [
            "vip_contacts",
            "feedback_type",
            "BRIEFING_PERSONALIZATION",
            "hide_newsletters",
            "mode_schedules",
        ]
        hits = [b for b in banned if b in text]
        if hits:
            record(
                "Daily Briefing UI layout unchanged",
                "FAIL",
                f"DailyBriefingBody contains personalization/feedback UI tokens: {hits}",
            )
        else:
            record(
                "Daily Briefing UI layout unchanged",
                "PASS",
                "DailyBriefingBody.tsx has no Task 5/6 preference/feedback controls; "
                f"onOpenEmail present={'onOpenEmail' in text}",
            )
    else:
        record("Daily Briefing UI layout unchanged", "FAIL", "DailyBriefingBody.tsx missing")

    if panel_path.exists():
        panel = panel_path.read_text(encoding="utf-8")
        hits = [b for b in ("vip_contacts", "feedback_type", "mode_schedules") if b in panel]
        if hits:
            record(
                "BriefingPanel UI unchanged",
                "FAIL",
                f"Unexpected tokens: {hits}",
            )
        else:
            record(
                "BriefingPanel UI unchanged",
                "PASS",
                "No Task 5/6 preference/feedback tokens in BriefingPanel.tsx",
            )

    # Live backend may still have old env if not restarted — probe via a
    # diagnostic if available; otherwise note.
    record(
        "Running backend flag reload",
        "NOT TESTED",
        "Cannot read uvicorn process env without instrumentation. "
        f"dotenv shows flags true; if preferences PATCH returns 403 personalization "
        f"disabled, restart backend to load .env lines 54–56.",
    )

    # Print table
    print("\n\n=== TESTING REPORT TABLE ===")
    print("| Feature | Status | Evidence | Issue |")
    print("|---|---|---|---|")
    for row in RESULTS:
        ev = row["evidence"].replace("|", "\\|").replace("\n", " ")
        iss = row["issue"].replace("|", "\\|").replace("\n", " ")
        print(f"| {row['feature']} | {row['status']} | {ev} | {iss} |")

    counts = {k: 0 for k in ("PASS", "FAIL", "BLOCKED", "NOT TESTED")}
    for row in RESULTS:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\nCounts:", counts)

    out_path = ROOT / "docs" / "TASKS_5_6_E2E_VERIFICATION_REPORT.md"
    lines = [
        "# Tasks 5 & 6 — End-to-End Verification Report",
        "",
        f"Generated by `backend/verify_tasks_5_6_e2e.py` against `{BACKEND}`.",
        "",
        "| Feature | Status | Evidence | Issue |",
        "|---|---|---|---|",
    ]
    for row in RESULTS:
        ev = row["evidence"].replace("|", "\\|").replace("\n", " ")
        iss = row["issue"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row['feature']} | {row['status']} | {ev} | {iss} |")
    lines.extend(
        [
            "",
            "## Counts",
            "",
            json.dumps(counts),
            "",
            "## Table status",
            "",
            "```json",
            json.dumps(table_status, indent=2),
            "```",
            "",
        ]
    )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0 if counts.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
