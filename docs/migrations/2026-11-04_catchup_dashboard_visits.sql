-- What Did I Miss? — last meaningful Home visit tracking (additive).
-- Run in Supabase SQL Editor when approved. Does not alter emails or daily_briefings.
-- Do not execute automatically.

CREATE TABLE IF NOT EXISTS public.briefing_dashboard_visits (
    gmail_account_email TEXT PRIMARY KEY,
    last_visit_at TIMESTAMPTZ,
    previous_visit_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS briefing_dashboard_visits_updated_idx
    ON public.briefing_dashboard_visits (updated_at DESC);

-- Optional short-lived catch-up result cache (same window reuse).
CREATE TABLE IF NOT EXISTS public.briefing_catchup_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    window_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT briefing_catchup_cache_account_window_uidx
        UNIQUE (gmail_account_email, window_key)
);

CREATE INDEX IF NOT EXISTS briefing_catchup_cache_expires_idx
    ON public.briefing_catchup_cache (expires_at);

ALTER TABLE public.briefing_dashboard_visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.briefing_catchup_cache ENABLE ROW LEVEL SECURITY;
