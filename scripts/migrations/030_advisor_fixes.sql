
CREATE SCHEMA IF NOT EXISTS extensions;
DO $$ BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = 'pg_trgm'
          AND n.nspname <> 'extensions'
    ) THEN
        ALTER EXTENSION pg_trgm SET SCHEMA extensions;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_chats_branch_id
    ON chats (branch_id);

CREATE INDEX IF NOT EXISTS idx_group_messages_owner_user_id
    ON group_messages (owner_user_id);


DO $$ BEGIN
    DROP POLICY IF EXISTS memory_nodes_user_policy ON memory_nodes;
    CREATE POLICY memory_nodes_user_policy ON memory_nodes
        FOR ALL USING (user_id = (SELECT current_setting('app.user_id', true)::bigint));

    DROP POLICY IF EXISTS memory_edges_user_policy ON memory_edges;
    CREATE POLICY memory_edges_user_policy ON memory_edges
        FOR ALL USING (user_id = (SELECT current_setting('app.user_id', true)::bigint));
END $$;
