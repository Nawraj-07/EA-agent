# AI Follow-up Assistant

Feature flag: `AI_FOLLOWUP_ASSISTANT_ENABLED` (default **false**).

## Architecture

1. **Detection** — `briefing_followup.py` + `ai_followup_assistant.py`
   - Reuses Task 2 thread classification.
   - When the assistant flag is on: threshold gating (24/48/72h), auto-reply awareness, uncertain vs definitive, stable `recommendation_key`, attempt counts from verified outgoing Gmail messages (never drafts).
2. **State** — Task 4 `briefing_recommendation_state` with `source_type='followup'` (metadata JSONB for attempts / waiting).
3. **Briefing merge** — same Priority Briefings `items[]` (kind=`followup`); no new dashboard panel.
4. **Background** — soft `refresh_followups_after_sync` after Gmail sync (existing scheduler; no second APScheduler).
5. **Actions** — existing recommendation draft / snooze(`tomorrow`) / dismiss APIs; also `GET /db/briefings/followups`.

## Enable (manual)

1. Apply `docs/migrations/2026-11-03_briefing_recommendation_state.sql` if missing (approved only).
2. Set `AI_FOLLOWUP_ASSISTANT_ENABLED=true` (and restart backend). Optionally keep `PENDING_REPLY_DETECTION_ENABLED` as needed.
3. Do **not** auto-send email or WhatsApp.

See `docs/migrations/2026-11-04_ai_followup_assistant_notes.sql` for persistence notes (no new DDL required for MVP).
