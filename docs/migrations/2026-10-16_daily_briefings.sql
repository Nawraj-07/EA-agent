-- Task 6.2: Daily briefing storage
CREATE TABLE IF NOT EXISTS public.daily_briefings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    email_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.daily_briefings ENABLE ROW LEVEL SECURITY;
