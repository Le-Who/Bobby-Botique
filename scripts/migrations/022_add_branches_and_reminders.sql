-- Migration 022: Add conversation_branches and user_reminders tables
-- Supports Feature 2 (Conversation Branching) and Feature 5 (Proactive Follow-ups)

-- ── Conversation branching ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversation_branches (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT 'auto',
    snapshot_history JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_branches_user
    ON conversation_branches (user_id, created_at DESC);

-- ── User reminders ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_reminders (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    trigger_at TIMESTAMPTZ NOT NULL,
    prompt TEXT NOT NULL,
    context_history JSONB,
    is_delivered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_reminders_pending
    ON user_reminders (trigger_at)
    WHERE is_delivered = FALSE;

CREATE INDEX IF NOT EXISTS idx_user_reminders_user
    ON user_reminders (user_id, created_at DESC);

-- RLS policies
ALTER TABLE conversation_branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_reminders ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'conversation_branches'
          AND policyname = 'branches_user_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY branches_user_policy ON conversation_branches
                FOR ALL USING (user_id = current_setting('app.current_user_id', true)::BIGINT)
        $policy$;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'user_reminders'
          AND policyname = 'reminders_user_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY reminders_user_policy ON user_reminders
                FOR ALL USING (user_id = current_setting('app.current_user_id', true)::BIGINT)
        $policy$;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'conversation_branches'
          AND policyname = 'branches_service_bypass'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY branches_service_bypass ON conversation_branches
                FOR ALL USING (current_setting('app.is_admin', true)::BOOLEAN = true)
        $policy$;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'user_reminders'
          AND policyname = 'reminders_service_bypass'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY reminders_service_bypass ON user_reminders
                FOR ALL USING (current_setting('app.is_admin', true)::BOOLEAN = true)
        $policy$;
    END IF;
END $$;
