-- Daily briefing account scope (for history API).
-- Run in Supabase SQL Editor after 2026-10-16_daily_briefings.sql.

ALTER TABLE public.daily_briefings
    ADD COLUMN IF NOT EXISTS gmail_account_email TEXT;

UPDATE public.daily_briefings
SET gmail_account_email = COALESCE(NULLIF(gmail_account_email, ''), 'legacy')
WHERE gmail_account_email IS NULL OR gmail_account_email = '';

ALTER TABLE public.daily_briefings
    ALTER COLUMN gmail_account_email SET NOT NULL,
    ALTER COLUMN gmail_account_email SET DEFAULT 'legacy';

CREATE INDEX IF NOT EXISTS daily_briefings_account_created_idx
    ON public.daily_briefings (gmail_account_email, created_at DESC);
