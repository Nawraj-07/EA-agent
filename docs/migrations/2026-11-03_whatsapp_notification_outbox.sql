-- WhatsApp notification outbox for Buddy AI (Meta Cloud API).
-- Run in Supabase SQL Editor. Does not drop or alter existing Buddy tables.

CREATE TABLE IF NOT EXISTS public.whatsapp_notification_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    notification_type TEXT NOT NULL
        CHECK (notification_type IN (
            'test',
            'daily_briefing',
            'important_email',
            'meeting_reminder',
            'payment_alert'
        )),
    recipient TEXT NOT NULL,
    source_id TEXT NOT NULL,
    template_name TEXT NOT NULL,
    template_language TEXT NOT NULL DEFAULT 'en_US',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued',
            'accepted',
            'sent',
            'delivered',
            'failed',
            'skipped',
            'pending_approval',
            'disabled',
            'auth_failed',
            'rate_limited'
        )),
    meta_message_id TEXT,
    error_code TEXT,
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    payload_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS whatsapp_outbox_dedupe_idx
    ON public.whatsapp_notification_outbox (notification_type, source_id, recipient);

CREATE INDEX IF NOT EXISTS whatsapp_outbox_status_created_idx
    ON public.whatsapp_notification_outbox (status, created_at DESC);

ALTER TABLE public.whatsapp_notification_outbox ENABLE ROW LEVEL SECURITY;
