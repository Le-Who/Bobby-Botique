
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'valid_from'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN valid_from TIMESTAMPTZ DEFAULT now();

        COMMENT ON COLUMN memory_edges.valid_from IS
            'When this fact became true (temporal graph lifecycle)';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'valid_to'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN valid_to TIMESTAMPTZ DEFAULT NULL;

        COMMENT ON COLUMN memory_edges.valid_to IS
            'When this fact was superseded (NULL = still current)';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_edges_temporal
    ON memory_edges (user_id, source_node, target_node)
    WHERE valid_to IS NULL;
