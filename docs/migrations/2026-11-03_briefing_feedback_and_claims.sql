-- Task 6: recommendation feedback, schedule claims, quality counters.
-- Additive only. Extends Task 4 recommendation tracking without rewriting briefings.
-- Run in Supabase SQL Editor when approved.

CREATE TABLE IF NOT EXISTS public.briefing_recommendation_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    recommendation_key TEXT NOT NULL,
    feedback_type TEXT NOT NULL
        CHECK (feedback_type IN (
            'useful',
            'not_useful',
            'incorrect_priority',
            'already_handled',
            'irrelevant'
        )),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT briefing_recommendation_feedback_account_key_uidx
        UNIQUE (gmail_account_email, recommendation_key)
);

CREATE INDEX IF NOT EXISTS briefing_recommendation_feedback_account_type_idx
    ON public.briefing_recommendation_feedback (
        gmail_account_email, feedback_type, updated_at DESC
    );

ALTER TABLE public.briefing_recommendation_feedback ENABLE ROW LEVEL SECURITY;

-- Cross-instance / restart-safe claim so the same period is not generated twice.
CREATE TABLE IF NOT EXISTS public.briefing_schedule_claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    job_kind TEXT NOT NULL
        CHECK (job_kind IN ('daily', 'evening', 'weekly', 'monday')),
    period_key TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT briefing_schedule_claims_uidx
        UNIQUE (gmail_account_email, job_kind, period_key)
);

CREATE INDEX IF NOT EXISTS briefing_schedule_claims_account_idx
    ON public.briefing_schedule_claims (gmail_account_email, claimed_at DESC);

ALTER TABLE public.briefing_schedule_claims ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS public.briefing_quality_counters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    metric_date DATE NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT briefing_quality_counters_account_date_uidx
        UNIQUE (gmail_account_email, metric_date)
);

CREATE INDEX IF NOT EXISTS briefing_quality_counters_account_idx
    ON public.briefing_quality_counters (gmail_account_email, metric_date DESC);

ALTER TABLE public.briefing_quality_counters ENABLE ROW LEVEL SECURITY;
