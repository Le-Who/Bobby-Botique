-- 025_add_temporal_graph_edges.sql
-- Add updated_at column to memory_edges for Time-decay traversal
-- Idempotency: All statements use IF NOT EXISTS / conditional guards — safe to re-run.

ALTER TABLE memory_edges
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Deduplicate existing memory_edges ONLY if the unique index does not yet exist.
-- Once the index is in place duplicates are structurally impossible, so re-running
-- this migration must NOT execute a blanket DELETE against production data.
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'memory_edges'
          AND indexname   = 'idx_memory_edges_unique'
    ) THEN
        -- One-time cleanup: keep the row with the highest weight (ties broken by id)
        DELETE FROM memory_edges
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER(
                           PARTITION BY user_id, source_node, target_node, predicate
                           ORDER BY weight DESC, id DESC
                       ) AS rn
                FROM memory_edges
            ) ranked
            WHERE rn > 1
        );
    END IF;
END $$;

-- Create unique index to allow ON CONFLICT Upserts
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_edges_unique
ON memory_edges(user_id, source_node, target_node, predicate);
