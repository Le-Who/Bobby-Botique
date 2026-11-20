
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'memory_edges'
          AND column_name  = 'is_core'
    ) THEN
        ALTER TABLE memory_edges
            ADD COLUMN is_core BOOLEAN NOT NULL DEFAULT FALSE;

        COMMENT ON COLUMN memory_edges.is_core IS
            'TRUE for identity/persona facts that must never decay (name, profession, allergies, etc.)';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_edges_core
    ON memory_edges (user_id, source_node, target_node)
    WHERE is_core = TRUE;
