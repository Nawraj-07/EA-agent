# Home Smart Briefing Filters + Auto-Generation

## Scheduler mapping (authoritative)

| Mode | Job id | Trigger | Storage |
|---|---|---|---|
| Morning | `daily_email_briefing` | `BRIEFING_HOUR_UTC`:`BRIEFING_MINUTE_UTC` UTC | `daily_briefings` |
| Evening | `smart_briefing_evening` | 17:00 local (`BRIEFING_TIMEZONE`) daily | `smart_briefing_mode_runs` |
| Weekly | `smart_briefing_weekly` | Saturday 17:00 local | `smart_briefing_mode_runs` |
| Monday | `smart_briefing_monday` | Monday 08:30 local | `smart_briefing_mode_runs` |

Task 3 env jobs are the sole automatic path for Evening/Weekly/Monday.
Do not enable Task 5 personalized `mode_schedules.*.enabled` for the same modes while Task 3 is on (registrar already skips duplicates).

Morning is never registered twice.

## Monday filter visibility

- Frontend uses configured IANA timezone from `GET /db/briefings/history` (`timezone`), default `Asia/Kolkata`.
- Monday chip is shown only when the local weekday in that zone is Monday.
- Default selected mode remains Morning even on Mondays.
- If the user selected Monday and the local day rolls to Tuesday, selection resets to Morning.
- Monday rows in `smart_briefing_mode_runs` are never deleted when the chip is hidden.

## Home fetch vs generate

- Filter switch → `GET /db/briefings/modes/{mode}/latest` only (no OpenAI).
- Morning → existing history day content.
- Regenerate Morning → `POST /internal/briefing-now?date=…`
- Regenerate mode → `POST /internal/briefings/modes/{mode}/generate`

## Migrations (do not apply without approval)

1. `docs/migrations/2026-11-03_smart_briefing_mode_runs.sql` — **required** for Evening/Weekly/Monday storage.
2. `docs/migrations/2026-11-03_briefing_feedback_and_claims.sql` — includes `briefing_schedule_claims` for cross-instance dedupe (apply if not already present).

## Environment (keep off until verified)

```env
ENABLE_SCHEDULER=true
BRIEFING_TIMEZONE=Asia/Kolkata
BRIEFING_HOUR_UTC=8
BRIEFING_MINUTE_UTC=0
SMART_BRIEFING_MODES_ENABLED=true
EVENING_BRIEFING_ENABLED=true
WEEKLY_DIGEST_ENABLED=true
MONDAY_PLANNING_ENABLED=true
BRIEFING_PERSONALIZATION_ENABLED=false
```

After enabling flags: restart the backend so APScheduler re-registers mode jobs.

Controlled pre-cron test (requires `ENABLE_MANUAL_SYNC=true`):

```http
POST /internal/briefings/modes/evening/generate
POST /internal/briefings/modes/weekly/generate
POST /internal/briefings/modes/monday/generate
```

## Live readiness blockers (verified 2026-11-04)

Automatic generation must **not** be claimed live until the items below are cleared.

Current probe:

| Check | Status |
|---|---|
| `smart_briefing_mode_runs` | **MISSING** — apply migration before enabling mode jobs |
| `briefing_schedule_claims` | EXISTS |
| `daily_briefings` | EXISTS |
| Mode env flags in `.env` | Not set (defaults false) — correct until migration + approval |
| `ENABLE_SCHEDULER` | true (Morning job only until mode flags enabled) |

Then:

1. Apply `docs/migrations/2026-11-03_smart_briefing_mode_runs.sql` (approval required).
2. Set Task 3 mode flags (approval required); keep `BRIEFING_PERSONALIZATION_ENABLED=false`.
3. Restart backend; confirm logs register `smart_briefing_evening|weekly|monday` once each.
4. Controlled `POST /internal/briefings/modes/{mode}/generate` + claim check.
5. Confirm Home filter switch only hits GET latest (no generate).
