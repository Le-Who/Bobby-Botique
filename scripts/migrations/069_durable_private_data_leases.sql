-- Migration 069: globally unique consent generations and durable provider-use leases.

CREATE SEQUENCE IF NOT EXISTS memory_consent_epoch_seq AS BIGINT;

-- Never move the sequence backwards when an idempotent migration runner rechecks
-- this file after live traffic has already allocated newer generations.
SELECT setval(
    'memory_consent_epoch_seq',
    GREATEST(
        (SELECT COALESCE(MAX(memory_epoch), 0) FROM chats),
        (SELECT last_value FROM memory_consent_epoch_seq)
    ),
    TRUE
);

ALTER TABLE chats
    ALTER COLUMN memory_epoch SET DEFAULT nextval('memory_consent_epoch_seq');

ALTER TABLE chats
    ADD COLUMN IF NOT EXISTS private_data_blocked BOOLEAN NOT NULL DEFAULT FALSE;

-- Replace migration 067's per-row increment. nextval is non-transactional by
-- design, so even a rolled-back attempt can never cause generation reuse.
CREATE OR REPLACE FUNCTION bump_memory_epoch_on_ltm_disable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.ltm_enabled IS TRUE
       AND NEW.ltm_enabled IS FALSE
       AND NEW.memory_epoch = OLD.memory_epoch THEN
        NEW.memory_epoch := nextval('memory_consent_epoch_seq');
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE IF NOT EXISTS private_data_leases (
    lease_id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    memory_epoch BIGINT NOT NULL,
    purpose TEXT NOT NULL CHECK (length(btrim(purpose)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS private_data_leases_user_epoch_idx
    ON private_data_leases (user_id, memory_epoch, purpose, expires_at);

CREATE INDEX IF NOT EXISTS private_data_leases_expiry_idx
    ON private_data_leases (expires_at);

ALTER TABLE private_data_leases ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'private_data_leases'
          AND policyname = 'private_data_leases_user_policy'
    ) THEN
        CREATE POLICY private_data_leases_user_policy ON private_data_leases
        FOR ALL USING (
            user_id = (SELECT NULLIF(current_setting('app.user_id', true), '')::bigint)
            OR (SELECT current_setting('app.is_admin', true)) = 'true'
        );
    END IF;
END;
$$;
