-- Task 5.4: allow approve-and-send claim status + store Gmail sent id
-- Run this in the Supabase SQL editor before using POST /drafts/{id}/approve
--
-- Claim path does NOT use an RPC. It runs a conditional UPDATE:
--   UPDATE drafts SET status = 'sending' WHERE id = $1 AND status = 'pending'
-- If 'sending' is missing from drafts_status_check, that UPDATE fails with 502.

-- 0) Ensure columns used by claim / mark-sent exist
ALTER TABLE public.drafts
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- 1) Persist Gmail message id after a successful send
ALTER TABLE public.drafts
    ADD COLUMN IF NOT EXISTS gmail_sent_message_id TEXT;

-- 2) Widen status check constraint to include sending + sent
--    Drop any existing status check constraint on drafts, then recreate.
DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'public'
          AND rel.relname = 'drafts'
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) ILIKE '%status%'
    LOOP
        EXECUTE format(
            'ALTER TABLE public.drafts DROP CONSTRAINT %I',
            constraint_name
        );
    END LOOP;
END $$;

ALTER TABLE public.drafts
    ADD CONSTRAINT drafts_status_check
    CHECK (
        status IN (
            'pending',
            'sending',
            'approved',
            'rejected',
            'sent'
        )
    );
