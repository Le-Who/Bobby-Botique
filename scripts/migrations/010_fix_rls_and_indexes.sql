

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


DROP INDEX IF EXISTS idx_chats_user_id;
DROP INDEX IF EXISTS idx_user_metrics_date;
DROP INDEX IF EXISTS idx_user_metrics_date_requestcount_user;
DROP INDEX IF EXISTS idx_user_metrics_userid_metricdate;
DROP INDEX IF EXISTS idx_user_metrics_user_date;
DROP INDEX IF EXISTS idx_group_messages_owner;


CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations (user_id);
