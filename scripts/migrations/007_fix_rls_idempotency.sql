-- Migration: Fix migration 006 idempotency
-- Description: Drops new-name policies before creating them, making 006 re-runnable.
-- Root cause: migration 006 dropped OLD policy names (roles_read_policy, roles_write_policy)
-- but created NEW names (roles_insert_policy, roles_update_policy, roles_delete_policy).
-- If 006 failed partway through, the new policies exist but schema_migrations has no record.

-- Drop all policies that migration 006 creates (idempotent cleanup)
DROP POLICY IF EXISTS users_policy ON users;
DROP POLICY IF EXISTS chats_policy ON chats;
DROP POLICY IF EXISTS user_documents_policy ON user_documents;
DROP POLICY IF EXISTS user_roles_policy ON user_roles;
DROP POLICY IF EXISTS conversations_policy ON conversations;
DROP POLICY IF EXISTS conversation_messages_policy ON conversation_messages;
DROP POLICY IF EXISTS group_chats_policy ON group_chats;
DROP POLICY IF EXISTS group_members_policy ON group_members;
DROP POLICY IF EXISTS group_messages_policy ON group_messages;
DROP POLICY IF EXISTS roles_read_policy ON roles;
DROP POLICY IF EXISTS roles_insert_policy ON roles;
DROP POLICY IF EXISTS roles_update_policy ON roles;
DROP POLICY IF EXISTS roles_delete_policy ON roles;
DROP POLICY IF EXISTS api_keys_policy ON api_keys;
DROP POLICY IF EXISTS key_usage_policy ON key_usage;
DROP POLICY IF EXISTS tavily_api_keys_policy ON tavily_api_keys;
DROP POLICY IF EXISTS tavily_key_usage_policy ON tavily_key_usage;
DROP POLICY IF EXISTS openrouter_api_keys_policy ON openrouter_api_keys;
DROP POLICY IF EXISTS openrouter_key_usage_policy ON openrouter_key_usage;
DROP POLICY IF EXISTS metrics_policy ON metrics;
DROP POLICY IF EXISTS error_logs_policy ON error_logs;

-- Recreate all optimized policies (same as 006)
CREATE POLICY users_policy ON users FOR ALL USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint OR 
    (select current_setting('app.is_admin', true)) = 'true'
);

CREATE POLICY chats_policy ON chats FOR ALL USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint OR 
    (select current_setting('app.is_admin', true)) = 'true'
);

CREATE POLICY user_documents_policy ON user_documents FOR ALL USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint OR 
    (select current_setting('app.is_admin', true)) = 'true'
);

CREATE POLICY user_roles_policy ON user_roles FOR ALL USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint OR 
    (select current_setting('app.is_admin', true)) = 'true'
);

CREATE POLICY conversations_policy ON conversations FOR ALL USING (
    user_id = NULLIF(current_setting('app.user_id', true), '')::bigint OR 
    (select current_setting('app.is_admin', true)) = 'true'
);

CREATE POLICY conversation_messages_policy ON conversation_messages FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR 
    EXISTS (
        SELECT 1 FROM conversations c 
        WHERE c.id = conversation_messages.conversation_id
          AND c.user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
    )
);

CREATE POLICY group_chats_policy ON group_chats FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm 
        WHERE gm.chat_id = group_chats.chat_id 
        AND gm.user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
    )
);

CREATE POLICY group_members_policy ON group_members FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm 
        WHERE gm.chat_id = group_members.chat_id 
        AND gm.user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
    )
);

CREATE POLICY group_messages_policy ON group_messages FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR
    EXISTS (
        SELECT 1 FROM group_members gm 
        WHERE gm.chat_id = group_messages.chat_id 
        AND gm.user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
    )
);

CREATE POLICY roles_read_policy ON roles FOR SELECT USING (true);
CREATE POLICY roles_insert_policy ON roles FOR INSERT WITH CHECK ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY roles_update_policy ON roles FOR UPDATE USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY roles_delete_policy ON roles FOR DELETE USING ((select current_setting('app.is_admin', true)) = 'true');

CREATE POLICY api_keys_policy ON api_keys FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY key_usage_policy ON key_usage FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY tavily_api_keys_policy ON tavily_api_keys FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY tavily_key_usage_policy ON tavily_key_usage FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY openrouter_api_keys_policy ON openrouter_api_keys FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY openrouter_key_usage_policy ON openrouter_key_usage FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY metrics_policy ON metrics FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
CREATE POLICY error_logs_policy ON error_logs FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');
