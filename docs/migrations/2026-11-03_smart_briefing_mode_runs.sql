-- Smart Briefing Modes 2.0 (evening / weekly / monday).
-- Separate from daily_briefings so mode runs never collide with Daily Briefing history.
-- Run in Supabase SQL Editor when approved. Does not alter daily_briefings.

CREATE TABLE IF NOT EXISTS public.smart_briefing_mode_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    mode TEXT NOT NULL
        CHECK (mode IN ('evening', 'weekly', 'monday')),
    period_key TEXT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    email_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'saved'
        CHECK (status IN ('saved', 'failed', 'empty')),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT smart_briefing_mode_runs_account_mode_period_key
        UNIQUE (gmail_account_email, mode, period_key)
);

CREATE INDEX IF NOT EXISTS smart_briefing_mode_runs_account_mode_created_idx
    ON public.smart_briefing_mode_runs (gmail_account_email, mode, created_at DESC);

ALTER TABLE public.smart_briefing_mode_runs ENABLE ROW LEVEL SECURITY;
