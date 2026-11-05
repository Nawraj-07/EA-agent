-- Ask About a Person — verified email aliases (additive).
-- Run in Supabase SQL Editor only when approved. Do not execute automatically.
-- Does not alter emails, email_analyses, or daily_briefings.
-- Unrelated contacts are never merged unless a row here is verified = true.

CREATE TABLE IF NOT EXISTS public.person_contact_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_account_email TEXT NOT NULL,
    canonical_email TEXT NOT NULL,
    alias_email TEXT NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT person_contact_aliases_account_alias_uidx
        UNIQUE (gmail_account_email, alias_email)
);

CREATE INDEX IF NOT EXISTS person_contact_aliases_account_idx
    ON public.person_contact_aliases (gmail_account_email, canonical_email);

ALTER TABLE public.person_contact_aliases ENABLE ROW LEVEL SECURITY;
