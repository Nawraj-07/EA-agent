-- Gmail sync ordering: account scope, thread metadata, inbox membership, sync cursor.
-- Run in Supabase SQL Editor after backup.

-- 1) New columns on emails
ALTER TABLE public.emails
    ADD COLUMN IF NOT EXISTS gmail_account_email TEXT,
    ADD COLUMN IF NOT EXISTS gmail_thread_id TEXT,
    ADD COLUMN IF NOT EXISTS internal_date BIGINT,
    ADD COLUMN IF NOT EXISTS label_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS in_inbox BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS analysis_status TEXT NOT NULL DEFAULT 'pending';

UPDATE public.emails
SET gmail_account_email = COALESCE(NULLIF(gmail_account_email, ''), 'legacy')
WHERE gmail_account_email IS NULL OR gmail_account_email = '';

ALTER TABLE public.emails
    ALTER COLUMN gmail_account_email SET NOT NULL,
    ALTER COLUMN gmail_account_email SET DEFAULT 'legacy';

-- Mark rows that already have analysis as done
UPDATE public.emails e
SET analysis_status = 'done'
WHERE EXISTS (
    SELECT 1 FROM public.email_analyses a WHERE a.email_id = e.id
)
AND analysis_status = 'pending';

ALTER TABLE public.emails
    DROP CONSTRAINT IF EXISTS emails_analysis_status_check;

ALTER TABLE public.emails
    ADD CONSTRAINT emails_analysis_status_check
    CHECK (analysis_status IN ('pending', 'done', 'failed'));

-- 2) Uniqueness: (account, gmail_message_id)
ALTER TABLE public.emails
    DROP CONSTRAINT IF EXISTS emails_gmail_message_id_key;

DROP INDEX IF EXISTS emails_gmail_message_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS emails_account_message_uidx
    ON public.emails (gmail_account_email, gmail_message_id);

CREATE INDEX IF NOT EXISTS emails_account_inbox_date_idx
    ON public.emails (gmail_account_email, in_inbox, internal_date DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS emails_account_thread_date_idx
    ON public.emails (gmail_account_email, gmail_thread_id, internal_date DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS emails_label_ids_gin_idx
    ON public.emails USING GIN (label_ids);

-- 3) Sync state (backfill cursor + history watermark)
CREATE TABLE IF NOT EXISTS public.gmail_sync_state (
    gmail_account_email TEXT PRIMARY KEY,
    history_id TEXT,
    backfill_page_token TEXT,
    backfill_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (backfill_status IN ('pending', 'running', 'complete')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.gmail_sync_state ENABLE ROW LEVEL SECURITY;
