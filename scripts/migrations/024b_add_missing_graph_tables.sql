-- 024b_add_missing_graph_tables.sql
-- Backfill: create GraphRAG tables that were originally created via the Supabase dashboard
-- and were missed in earlier migration backfills.

CREATE TABLE IF NOT EXISTS memory_nodes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    description TEXT,
    embedding halfvec(768),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, entity_name)
);

CREATE TABLE IF NOT EXISTS memory_edges (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    source_node BIGINT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    target_node BIGINT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    weight FLOAT DEFAULT 1.0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
