-- Migration 030: Fixes from Supabase Query Advisor
-- Purpose: Move extensions to 'extensions' schema, add covering indexes for foreign keys,
--          and wrap current_setting() in SELECT for RLS policies (auth_rls_initplan).

-- 1. Security: Move pg_trgm to extensions schema
CREATE SCHEMA IF NOT EXISTS extensions;
ALTER EXTENSION pg_trgm SET SCHEMA extensions;

-- 2. Performance: Add covering indexes for foreign keys
CREATE INDEX IF NOT EXISTS idx_chats_branch_id
    ON chats (branch_id);

CREATE INDEX IF NOT EXISTS idx_group_messages_owner_user_id
    ON group_messages (owner_user_id);


-- 3. Performance: Fix auth_rls_initplan for memory_nodes and memory_edges
DO $$ BEGIN
    DROP POLICY IF EXISTS memory_nodes_user_policy ON memory_nodes;
    CREATE POLICY memory_nodes_user_policy ON memory_nodes
        FOR ALL USING (user_id = (SELECT current_setting('app.user_id', true)::bigint));

    DROP POLICY IF EXISTS memory_edges_user_policy ON memory_edges;
    CREATE POLICY memory_edges_user_policy ON memory_edges
        FOR ALL USING (user_id = (SELECT current_setting('app.user_id', true)::bigint));
END $$;
