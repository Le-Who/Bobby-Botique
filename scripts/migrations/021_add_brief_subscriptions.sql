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

CREATE POLICY brief_subscriptions_user_policy ON brief_subscriptions
    FOR ALL
    USING (user_id = current_setting('app.current_user_id', true)::bigint)
    WITH CHECK (user_id = current_setting('app.current_user_id', true)::bigint);

CREATE POLICY brief_subscriptions_admin_policy ON brief_subscriptions
    FOR ALL
    USING (current_setting('app.is_admin', true)::boolean = true)
    WITH CHECK (current_setting('app.is_admin', true)::boolean = true);
