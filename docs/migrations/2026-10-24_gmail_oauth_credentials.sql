-- Encrypted Gmail OAuth credentials (backend Fernet ciphertext only).
-- Do not store plaintext refresh/access tokens.
-- RLS enabled with no anon/authenticated policies: service_role only.

CREATE TABLE IF NOT EXISTS public.gmail_oauth_credentials (
    gmail_account_email TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    key_version SMALLINT NOT NULL DEFAULT 1,
    token_expiry TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.gmail_oauth_credentials ENABLE ROW LEVEL SECURITY;
