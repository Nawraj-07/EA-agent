-- End My Day closing checkpoints (additive).
-- Run in Supabase SQL Editor when approved. Does not alter emails,
-- daily_briefings, recommendation state, or Gmail labels.
-- Do not execute automatically.

CREATE TABLE IF NOT EXISTS public.end_my_day_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    local_date DATE NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    wrapup_source TEXT NOT NULL DEFAULT 'stored_mail',
    item_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
    unresolved JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT end_my_day_checkpoints_account_date_uidx
        UNIQUE (gmail_account_email, local_date)
);

CREATE INDEX IF NOT EXISTS end_my_day_checkpoints_account_idx
    ON public.end_my_day_checkpoints (gmail_account_email, local_date DESC);

ALTER TABLE public.end_my_day_checkpoints ENABLE ROW LEVEL SECURITY;
