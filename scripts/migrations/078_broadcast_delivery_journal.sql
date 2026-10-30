-- Telegram receipts are observations, never proof of exactly-once delivery.
CREATE TABLE IF NOT EXISTS public.broadcast_journal_coverage (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
INSERT INTO public.broadcast_journal_coverage(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS public.broadcast_batches (
    batch_id UUID PRIMARY KEY,
    operation_id UUID NOT NULL UNIQUE,
    parent_batch_id UUID REFERENCES public.broadcast_batches(batch_id),
    task_id TEXT,
    admission_state TEXT NOT NULL DEFAULT 'unknown' CHECK (admission_state IN ('accepted','existing','rejected','unknown')),
    admission_reason TEXT NOT NULL DEFAULT 'admission_not_confirmed',
    storage_mode TEXT NOT NULL DEFAULT 'unavailable',
    channel TEXT NOT NULL CHECK (channel IN ('daily_challenge','horoscope','tarot')),
    mode TEXT NOT NULL CHECK (mode IN ('offer','delivery')),
    effective_date DATE NOT NULL,
    variant TEXT NOT NULL DEFAULT '',
    force_offer BOOLEAN NOT NULL DEFAULT FALSE,
    scheduled BOOLEAN NOT NULL DEFAULT FALSE,
    settings_revision BIGINT,
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','queued','running','finished')),
    owner_token UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    total INTEGER NOT NULL CHECK (total BETWEEN 0 AND 100),
    pending INTEGER NOT NULL DEFAULT 0 CHECK (pending >= 0),
    started INTEGER NOT NULL DEFAULT 0 CHECK (started >= 0),
    sent INTEGER NOT NULL DEFAULT 0 CHECK (sent >= 0),
    failed INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0),
    skipped INTEGER NOT NULL DEFAULT 0 CHECK (skipped >= 0),
    unknown_delivery INTEGER NOT NULL DEFAULT 0 CHECK (unknown_delivery >= 0),
    retryable_failed INTEGER NOT NULL DEFAULT 0 CHECK (retryable_failed >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS broadcast_batches_task_idx ON public.broadcast_batches(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS broadcast_batches_channel_created_idx ON public.broadcast_batches(channel,created_at);
CREATE INDEX IF NOT EXISTS broadcast_batches_open_idx ON public.broadcast_batches(created_at) WHERE state<>'finished';

CREATE TABLE IF NOT EXISTS public.broadcast_recipients (
    batch_id UUID NOT NULL REFERENCES public.broadcast_batches(batch_id) ON DELETE CASCADE,
    recipient_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    privacy_epoch BIGINT NOT NULL,
    subscription_fingerprint TEXT,
    delivery_claimed_at TIMESTAMPTZ,
    slot_date DATE,
    schedule_key TEXT UNIQUE,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','started','sent','failed','skipped','unknown_delivery')),
    owner_token UUID,
    reason_code TEXT CHECK (reason_code ~ '^[a-z0-9_]{1,64}$'),
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(batch_id,recipient_id,channel)
);
CREATE INDEX IF NOT EXISTS broadcast_recipients_batch_state_idx ON public.broadcast_recipients(batch_id,state);
CREATE INDEX IF NOT EXISTS broadcast_recipients_retention_idx ON public.broadcast_recipients(created_at);

CREATE TABLE IF NOT EXISTS public.broadcast_delivery_attempts (
    attempt_id UUID PRIMARY KEY,
    batch_id UUID NOT NULL,
    recipient_id BIGINT NOT NULL,
    channel TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    operation_id UUID NOT NULL UNIQUE,
    owner_token UUID NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    outcome TEXT NOT NULL DEFAULT 'started' CHECK (outcome IN ('started','sent','failed','skipped','unknown_delivery')),
    reason_code TEXT CHECK (reason_code ~ '^[a-z0-9_]{1,64}$'),
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    message_id BIGINT,
    effective_date DATE NOT NULL,
    settings_revision BIGINT,
    FOREIGN KEY(batch_id,recipient_id,channel) REFERENCES public.broadcast_recipients(batch_id,recipient_id,channel) ON DELETE CASCADE,
    UNIQUE(batch_id,recipient_id,channel,sequence)
);
CREATE INDEX IF NOT EXISTS broadcast_attempts_batch_state_idx ON public.broadcast_delivery_attempts(batch_id,outcome);
CREATE INDEX IF NOT EXISTS broadcast_attempts_channel_created_idx ON public.broadcast_delivery_attempts(channel,started_at);

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['broadcast_journal_coverage','broadcast_batches','broadcast_recipients','broadcast_delivery_attempts']
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', table_name);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='public' AND tablename=table_name AND policyname=table_name || '_admin_policy') THEN
            EXECUTE format('CREATE POLICY %I ON public.%I FOR ALL USING ((SELECT current_setting(''app.is_admin'',true))=''true'') WITH CHECK ((SELECT current_setting(''app.is_admin'',true))=''true'')', table_name || '_admin_policy', table_name);
        END IF;
    END LOOP;
END $$;
