
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

ALTER TABLE key_model_status ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'service_role'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'key_model_status'
          AND policyname = 'key_model_status_service_role'
    ) THEN
        EXECUTE 'CREATE POLICY key_model_status_service_role ON key_model_status FOR ALL TO service_role USING (true) WITH CHECK (true)';
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'key_model_status'
          AND policyname = 'key_model_status_admin_policy'
    ) THEN
        EXECUTE format(
            'CREATE POLICY key_model_status_admin_policy ON key_model_status FOR ALL USING (
                current_setting(''app.is_admin'', true) = ''true''
            ) WITH CHECK (
                current_setting(''app.is_admin'', true) = ''true''
            )'
        );
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_key_model_status_lookup
    ON key_model_status (key_hash, model_name, status);
