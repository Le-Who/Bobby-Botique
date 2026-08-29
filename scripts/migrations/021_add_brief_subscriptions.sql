-- Scheduled Intelligence Briefs: subscription table
-- Users can subscribe to receive periodic AI-curated summaries
-- based on their LTM data + web search.

CREATE TABLE IF NOT EXISTS brief_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    subscription_type TEXT NOT NULL DEFAULT 'morning_brief',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    preferred_hour SMALLINT NOT NULL DEFAULT 7,
    last_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, subscription_type)
);

-- Index for the scheduler: find active subscriptions that need to run now
CREATE INDEX IF NOT EXISTS idx_briefs_active
    ON brief_subscriptions(is_active, preferred_hour)
    WHERE is_active = TRUE;

-- RLS
ALTER TABLE brief_subscriptions ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'brief_subscriptions'
          AND policyname = 'brief_subscriptions_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY brief_subscriptions_policy ON brief_subscriptions
                FOR ALL
                USING (
                    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
                    OR current_setting('app.is_admin', true) = 'true'
                )
                WITH CHECK (
                    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
                    OR current_setting('app.is_admin', true) = 'true'
                )
        $policy$;
    END IF;
END $$;
