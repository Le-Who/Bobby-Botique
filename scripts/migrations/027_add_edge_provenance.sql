
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'source_memory_ids'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN source_memory_ids BIGINT[] NOT NULL DEFAULT '{}';

        COMMENT ON COLUMN memory_edges.source_memory_ids IS
            'IDs of long_term_memory rows from which this edge was extracted (HippoRAG 2 provenance)';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_edges_source_ids
    ON memory_edges USING gin (source_memory_ids)
    WHERE source_memory_ids != '{}';
