-- Per-calendar-day briefing key (Generate for selected history date).
-- Run after 2026-10-19_briefing_account_history.sql.

ALTER TABLE public.daily_briefings
    ADD COLUMN IF NOT EXISTS briefing_date DATE;

-- Backfill from created_at in Asia/Kolkata (app default BRIEFING_TIMEZONE)
UPDATE public.daily_briefings
SET briefing_date = (created_at AT TIME ZONE 'Asia/Kolkata')::date
WHERE briefing_date IS NULL;

ALTER TABLE public.daily_briefings
    ALTER COLUMN briefing_date SET NOT NULL;

CREATE INDEX IF NOT EXISTS daily_briefings_account_date_idx
    ON public.daily_briefings (gmail_account_email, briefing_date DESC, created_at DESC);
