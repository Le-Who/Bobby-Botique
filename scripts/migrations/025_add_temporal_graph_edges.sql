
ALTER TABLE memory_edges
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename  = 'memory_edges'
          AND indexname   = 'idx_memory_edges_unique'
    ) THEN
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_edges_unique
ON memory_edges(user_id, source_node, target_node, predicate);
