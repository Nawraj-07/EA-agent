
CREATE TABLE IF NOT EXISTS public.emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_message_id TEXT NOT NULL UNIQUE,
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    snippet TEXT,
    body TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.email_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id UUID NOT NULL UNIQUE
        REFERENCES public.emails(id) ON DELETE CASCADE,
    summary TEXT,
    category TEXT,
    importance TEXT,
    deadlines JSONB DEFAULT '[]'::jsonb,
    action_required BOOLEAN,
    confidence DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id UUID NOT NULL
        REFERENCES public.emails(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            status IN (
                'pending',
                'sending',
                'approved',
                'rejected',
                'sent'
            )
        ),
    gmail_sent_message_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.daily_briefings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    email_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_analyses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_briefings ENABLE ROW LEVEL SECURITY;
