-- Task 5: account-scoped briefing preferences.
-- Additive only. Does not alter daily_briefings / emails / email_analyses.
-- Run in Supabase SQL Editor when approved.

CREATE TABLE IF NOT EXISTS public.briefing_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    working_days JSONB NOT NULL DEFAULT '[1,2,3,4,5]'::jsonb,
    working_hours JSONB NOT NULL DEFAULT '{"start":"09:00","end":"18:00"}'::jsonb,
    briefing_sections JSONB NOT NULL DEFAULT '{}'::jsonb,
    vip_contacts JSONB NOT NULL DEFAULT '[]'::jsonb,
    filtering_preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    mode_schedules JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT briefing_preferences_account_uidx UNIQUE (gmail_account_email)
);

CREATE INDEX IF NOT EXISTS briefing_preferences_account_idx
    ON public.briefing_preferences (gmail_account_email);

ALTER TABLE public.briefing_preferences ENABLE ROW LEVEL SECURITY;
