# What Did I Miss?

Feature flag: `WHAT_DID_I_MISS_ENABLED` (default **false**).

## Architecture

1. User clicks **What Did I Miss?** on Home (Daily Briefing header).
2. `POST /db/catchup/generate` resolves the absence window from:
   - optional manual start, else
   - `briefing_dashboard_visits.last_visit_at`, else
   - default lookback (`CATCHUP_DEFAULT_LOOKBACK_HOURS`, default 72).
3. Visit timestamps are **not** updated during generate (refresh-safe).
4. Counts come from stored `emails` + `email_analyses` (incoming only).
5. Follow-ups reuse AI Follow-up Assistant detection + snooze/dismiss state.
6. Optional OpenAI synthesis for summary text only (never invents counts).
7. Closing the panel calls `POST /db/catchup/record-visit` to advance last visit.

## Migration (approval required)

`docs/migrations/2026-11-04_catchup_dashboard_visits.sql`

Creates:
- `briefing_dashboard_visits`
- `briefing_catchup_cache`

Do not auto-run. Feature works with default lookback even if visit table is missing; record-visit returns 503 until migrated.

## APIs

| Method | Path | Role |
|---|---|---|
| GET | `/db/catchup/status` | `{enabled, default_lookback_hours}` |
| POST | `/db/catchup/generate` | Catch-up JSON (`start`/`end`/`force_refresh` optional) |
| POST | `/db/catchup/record-visit` | Persist meaningful visit after viewing |

## Enable (manual)

1. Apply migration when approved.
2. Set `WHAT_DID_I_MISS_ENABLED=true` and recreate backend.
3. Rebuild frontend if not volume-mounted.
