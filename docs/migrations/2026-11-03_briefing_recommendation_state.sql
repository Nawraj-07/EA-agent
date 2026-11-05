-- Interactive Briefing recommendation state (Task 4).
-- Separate from daily_briefings.content so regenerating briefings does not wipe user status.
-- Run in Supabase SQL Editor when approved. Does not alter daily_briefings.

CREATE TABLE IF NOT EXISTS public.briefing_recommendation_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('briefing_item', 'meeting', 'deadline', 'followup')),
    source_id TEXT NOT NULL,
    recommendation_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed', 'snoozed', 'dismissed')),
    snoozed_until TIMESTAMPTZ,
    title TEXT,
    action_type TEXT
        CHECK (action_type IS NULL OR action_type IN (
            'view_email',
            'draft_reply',
            'snooze',
            'mark_complete',
            'dismiss'
        )),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT briefing_recommendation_state_account_key_uidx
        UNIQUE (gmail_account_email, recommendation_key)
);

CREATE INDEX IF NOT EXISTS briefing_recommendation_state_account_status_idx
    ON public.briefing_recommendation_state (gmail_account_email, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS briefing_recommendation_state_snooze_idx
    ON public.briefing_recommendation_state (gmail_account_email, snoozed_until)
    WHERE status = 'snoozed';

ALTER TABLE public.briefing_recommendation_state ENABLE ROW LEVEL SECURITY;
