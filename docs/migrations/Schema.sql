
CREATE TABLE IF NOT EXISTS public.emails (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL DEFAULT 'legacy',
    gmail_message_id TEXT NOT NULL,
    gmail_thread_id TEXT,
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    snippet TEXT,
    body TEXT,
    internal_date BIGINT,
    label_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    in_inbox BOOLEAN NOT NULL DEFAULT true,
    analysis_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (analysis_status IN ('pending', 'done', 'failed')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (gmail_account_email, gmail_message_id)
);

CREATE INDEX IF NOT EXISTS emails_account_inbox_date_idx
    ON public.emails (gmail_account_email, in_inbox, internal_date DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS emails_account_thread_date_idx
    ON public.emails (gmail_account_email, gmail_thread_id, internal_date DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS emails_label_ids_gin_idx
    ON public.emails USING GIN (label_ids);

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
    gmail_account_email TEXT NOT NULL DEFAULT 'legacy',
    briefing_date DATE NOT NULL,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    email_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS daily_briefings_account_created_idx
    ON public.daily_briefings (gmail_account_email, created_at DESC);

CREATE INDEX IF NOT EXISTS daily_briefings_account_date_idx
    ON public.daily_briefings (gmail_account_email, briefing_date DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS public.gmail_sync_state (
    gmail_account_email TEXT PRIMARY KEY,
    history_id TEXT,
    backfill_page_token TEXT,
    backfill_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (backfill_status IN ('pending', 'running', 'complete')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.gmail_oauth_credentials (
    gmail_account_email TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    key_version SMALLINT NOT NULL DEFAULT 1,
    token_expiry TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.emails ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_analyses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_briefings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gmail_sync_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.gmail_oauth_credentials ENABLE ROW LEVEL SECURITY;
