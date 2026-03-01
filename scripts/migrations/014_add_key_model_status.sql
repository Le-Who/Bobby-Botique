-- Migration 014: Per-model key status tracking
-- Tracks (key_hash, model_name) health to prevent invalid keys from
-- poisoning all requests, while supporting auto-recovery via cooldowns.

CREATE TABLE IF NOT EXISTS key_model_status (
    key_hash       TEXT NOT NULL REFERENCES api_keys(key_hash) ON DELETE CASCADE,
    model_name     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'suspended')),
    suspended_until TIMESTAMPTZ,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (key_hash, model_name)
);

-- Enable RLS (consistent with other tables)
ALTER TABLE key_model_status ENABLE ROW LEVEL SECURITY;

-- Service-role full access policy
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'key_model_status' AND policyname = 'key_model_status_service_role'
    ) THEN
        EXECUTE 'CREATE POLICY key_model_status_service_role ON key_model_status FOR ALL TO service_role USING (true) WITH CHECK (true)';
    END IF;
END
$$;

-- Admin access policy
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE tablename = 'key_model_status' AND policyname = 'key_model_status_admin'
    ) THEN
        EXECUTE format(
            'CREATE POLICY key_model_status_admin ON key_model_status FOR ALL USING (
                current_setting(''app.user_is_admin'', true)::boolean = true
            ) WITH CHECK (
                current_setting(''app.user_is_admin'', true)::boolean = true
            )'
        );
    END IF;
END
$$;

-- Index for efficient lookups by key_hash + model
CREATE INDEX IF NOT EXISTS idx_key_model_status_lookup
    ON key_model_status (key_hash, model_name, status);
