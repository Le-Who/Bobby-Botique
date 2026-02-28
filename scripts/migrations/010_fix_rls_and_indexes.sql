-- Migration 010: Fix broken RLS policies and cleanup unused indexes
-- Applied via Supabase MCP on 2026-02-28
--
-- Fixes:
--   C1: 3 RLS policies using wrong variable (app.current_user_id → app.user_id)
--   C2: Wrap all user_id checks in (select ...) for InitPlan optimization
--   H2: Drop redundant idx_chats_user_id (PK already covers)
--   H3: Drop 5 unused indexes (4 on user_metrics + 1 on group_messages)
--   H4: Add missing conversations.user_id index

-- ── PART 1: Fix broken policies (feedback, user_metrics, user_state) ────────

DROP POLICY IF EXISTS feedback_policy ON feedback;
CREATE POLICY feedback_policy ON feedback FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS user_metrics_policy ON user_metrics;
CREATE POLICY user_metrics_policy ON user_metrics FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS user_state_policy ON user_state;
CREATE POLICY user_state_policy ON user_state FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

-- ── PART 2: Optimize existing policies with full InitPlan wrapping ───────────

DROP POLICY IF EXISTS users_policy ON users;
CREATE POLICY users_policy ON users FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS chats_policy ON chats;
CREATE POLICY chats_policy ON chats FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS user_documents_policy ON user_documents;
CREATE POLICY user_documents_policy ON user_documents FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS user_roles_policy ON user_roles;
CREATE POLICY user_roles_policy ON user_roles FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS conversations_policy ON conversations;
CREATE POLICY conversations_policy ON conversations FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS active_chat_messages_policy ON active_chat_messages;
CREATE POLICY active_chat_messages_policy ON active_chat_messages FOR ALL USING (
    user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint) OR
    (select current_setting('app.is_admin', true)) = 'true'
);

DROP POLICY IF EXISTS conversation_messages_policy ON conversation_messages;
CREATE POLICY conversation_messages_policy ON conversation_messages FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true'
    OR owner_user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
);

DROP POLICY IF EXISTS group_chats_policy ON group_chats;
CREATE POLICY group_chats_policy ON group_chats FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm
        WHERE gm.chat_id = group_chats.chat_id
        AND gm.user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
    )
);

DROP POLICY IF EXISTS group_members_policy ON group_members;
CREATE POLICY group_members_policy ON group_members FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm
        WHERE gm.chat_id = group_members.chat_id
        AND gm.user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
    )
);

DROP POLICY IF EXISTS group_messages_policy ON group_messages;
CREATE POLICY group_messages_policy ON group_messages FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm
        WHERE gm.chat_id = group_messages.chat_id
        AND gm.user_id = (select NULLIF(current_setting('app.user_id', true), '')::bigint)
    )
);

DROP POLICY IF EXISTS model_configuration_policy ON model_configuration;
CREATE POLICY model_configuration_policy ON model_configuration FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true'
);

-- ── PART 3: Cleanup unused/redundant indexes ────────────────────────────────

DROP INDEX IF EXISTS idx_chats_user_id;
DROP INDEX IF EXISTS idx_user_metrics_date;
DROP INDEX IF EXISTS idx_user_metrics_date_requestcount_user;
DROP INDEX IF EXISTS idx_user_metrics_userid_metricdate;
DROP INDEX IF EXISTS idx_user_metrics_user_date;
DROP INDEX IF EXISTS idx_group_messages_owner;

-- ── PART 4: Add missing index ───────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations (user_id);
