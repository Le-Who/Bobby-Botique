
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'predicate_embedding'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN predicate_embedding halfvec(768);

        COMMENT ON COLUMN memory_edges.predicate_embedding IS
            'Embedding of the predicate phrase for semantic deduplication at consolidation time';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_edges_pred_embed
    ON memory_edges USING hnsw (predicate_embedding halfvec_cosine_ops)
    WITH (m = 8, ef_construction = 32)
    WHERE predicate_embedding IS NOT NULL;
