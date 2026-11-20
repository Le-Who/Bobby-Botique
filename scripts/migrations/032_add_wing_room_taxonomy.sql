
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'long_term_memory'
          AND column_name = 'wing'
    ) THEN
        ALTER TABLE long_term_memory ADD COLUMN wing TEXT DEFAULT NULL;
        COMMENT ON COLUMN long_term_memory.wing IS
            'MemPalace wing: identity, projects, social, knowledge, temporal';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'long_term_memory'
          AND column_name = 'room'
    ) THEN
        ALTER TABLE long_term_memory ADD COLUMN room TEXT DEFAULT NULL;
        COMMENT ON COLUMN long_term_memory.room IS
            'MemPalace room within wing (e.g., bio, prefs, active, family)';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'long_term_memory'
          AND column_name = 'hall_type'
    ) THEN
        ALTER TABLE long_term_memory ADD COLUMN hall_type TEXT DEFAULT NULL;
        COMMENT ON COLUMN long_term_memory.hall_type IS
            'Content type within room: fact, opinion, event, plan, preference';
    END IF;
END $$;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'memory_nodes'
          AND column_name = 'wing'
    ) THEN
        ALTER TABLE memory_nodes ADD COLUMN wing TEXT DEFAULT NULL;
        COMMENT ON COLUMN memory_nodes.wing IS
            'MemPalace wing classification for entity nodes';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'memory_nodes'
          AND column_name = 'room'
    ) THEN
        ALTER TABLE memory_nodes ADD COLUMN room TEXT DEFAULT NULL;
        COMMENT ON COLUMN memory_nodes.room IS
            'MemPalace room classification for entity nodes';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ltm_taxonomy
    ON long_term_memory (user_id, wing, room)
    WHERE wing IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_nodes_taxonomy
    ON memory_nodes (user_id, wing)
    WHERE wing IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ltm_wing_identity_hnsw
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WHERE wing = 'identity';

CREATE INDEX IF NOT EXISTS idx_ltm_wing_projects_hnsw
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WHERE wing = 'projects';
