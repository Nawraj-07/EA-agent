# AI Follow-up Assistant — Workflow E2E Report

Generated: 2026-11-04 (local verification)

## Summary

Controlled workflow verification used **isolated `TEST-FOLLOWUP-WF*` DB fixtures** (no Gmail send, no WhatsApp, no Gmail timestamp edits). Live APIs + Supabase were checked. One auto-resolve bug was found and fixed mid-run.

**Verdict:** Workflow stages for draft / snooze / dismiss / resolution **PASS** on isolated data. Live SENT-mailbox coverage remains **partial** (INBOX-primary sync).

## Stage results

| Stage | Result | Evidence | Issue |
|---|---|---|---|
| 1 Outgoing sync pipeline | **PASS** | `from_account_count=28`, SENT labels present on samples; thread_id / internal_date / sender available | — |
| 1b SENT mailbox coverage | **PASS*** | Partial outgoing rows exist | Full SENT-only mailbox not synced (INBOX-primary). Pure SENT-only threads may be missing |
| 2 Thread metadata + direction | **PASS** | Isolated row direction=`outgoing`, labels=`['SENT']`, thread + timestamp set | — |
| 3 Detection on briefing path | **PASS** | Flag on; `awaiting_response` detected for 60h ask | — |
| 4 Unanswered outgoing (isolated) | **PASS** | Title `Follow up: TEST-FOLLOWUP-WF…` | — |
| 5 Threshold eligibility | **PASS** | 2h ask → uncertain/not active; medium threshold 48h | — |
| 6 Persist recommendation | **PASS** | `briefing_recommendation_state` upsert `pending` id `1df16768-…` | — |
| 7 Home briefing surface | **PASS** | Injected into latest `daily_briefings.content.items` with `kind=followup` + actions flags | Natural UI needs regenerate/sync; not a layout change |
| 8 Draft Follow-up (no send) | **PASS** | `ok=True`, `attempt_counted=False`, draft `status=pending`, `gmail_sent_message_id=null` | — |
| 9 Remind Tomorrow | **PASS** | DB `status=snoozed` + `snoozed_until` set | — |
| 10 Dismiss | **PASS** | API + DB `dismissed` | — |
| 11 Resolve after human reply | **PASS** | No longer active; DB `completed` | Initially `resolved_keys=[]` (bug); fixed — retest shows keys populated |
| 12 Resolved not shown | **PASS** | `active_for_key=[]` | — |
| 13 Mode filters | **PASS** | `/db/briefings/modes` evening/weekly/monday; history timezone/days | — |
| Duplicate prevention | **PASS** | 1 row per recommendation_key | — |

\*Partial coverage caveat — treat comprehensive live SENT-only detection as limited until SENT ingest is expanded (out of scope unless approved).

## Live API (redacted)

```json
{
  "enabled": true,
  "assistant": "ai_followup",
  "recommendations": [ /* pending_reply / followup items from latest briefing */ ]
}
```

Draft response shape:

```json
{
  "ok": true,
  "attempt_counted": false,
  "draft": { "id": "6c611b1a-…", "status": "pending" },
  "note": "Draft saved for review. Nothing was sent. …"
}
```

## Bug found + fix

**Bug:** After a human reply, classifier often returns `informational` (not `resolved`), so `resolved_keys` stayed empty and recommendation rows could remain `pending`.

**Fix:** `briefing_followup.detect_followups_for_account_sync` now emits `resolved_keys` when a relevant non-auto incoming reply exists after an outgoing ask (not only when state == `resolved`).

Retest: `resolved_keys=['f4eccf5c…']`, `active=0`.

## Frontend

Follow-up actions render inside existing **Priority Briefings** cards when `kind=followup` / `followup_state` and `followup_actions_enabled` are set. No separate panel; layout unchanged.

## Safety

- No automatic email send
- No WhatsApp triggered
- No Gmail OAuth scope changes
- Isolated fixtures cleaned up after run
