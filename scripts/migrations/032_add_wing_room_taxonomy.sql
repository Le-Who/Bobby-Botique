-- Migration 032: Add Wing/Room taxonomy to long_term_memory and memory_nodes
-- Purpose: MemPalace-inspired hierarchical classification of memories.
--          Wing = high-level category, Room = subcategory, Hall = content type.
--          Enables targeted retrieval (e.g., only search "identity" memories)
--          and partial HNSW indexes for high-traffic wings.
--
-- Wings: identity, projects, social, knowledge, temporal
-- Idempotency: All statements guarded by IF NOT EXISTS / conditional checks.

-- Step 1: Add taxonomy columns to long_term_memory
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

-- Step 2: Add taxonomy columns to memory_nodes
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

-- Step 3: B-tree composite index for taxonomy filtering
CREATE INDEX IF NOT EXISTS idx_ltm_taxonomy
    ON long_term_memory (user_id, wing, room)
    WHERE wing IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_nodes_taxonomy
    ON memory_nodes (user_id, wing)
    WHERE wing IS NOT NULL;

-- Step 4: Partial HNSW indexes for high-traffic wings
-- These accelerate vector search within a single wing dramatically.
-- pgvector supports partial HNSW indexes with WHERE clause (verified v0.7+).
CREATE INDEX IF NOT EXISTS idx_ltm_wing_identity_hnsw
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WHERE wing = 'identity';

CREATE INDEX IF NOT EXISTS idx_ltm_wing_projects_hnsw
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WHERE wing = 'projects';
