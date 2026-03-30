-- Migration 026: Add is_core flag to memory_edges for Core Persona Facts
-- Purpose: Allows the GraphRAG retrieval engine to distinguish "eternal" identity
--          facts (name, allergies, profession) from short-lived facts.
--          Core edges bypass the time-decay weighting formula in traversal queries,
--          so they always appear at the top regardless of edge age.
-- Idempotency: All statements use IF NOT EXISTS / are safe to re-run.
-- Atomicity: Wrapped in DO $$ block consistent with migration 024 pattern.

DO $$ BEGIN
    -- Step 1: Add is_core column if not already present
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

-- Step 2: Partial index — fast lookup of only core edges (avoids full-scan on cold cache)
-- CREATE INDEX IF NOT EXISTS is always idempotent (Postgres 9.5+).
CREATE INDEX IF NOT EXISTS idx_memory_edges_core
    ON memory_edges (user_id, source_node, target_node)
    WHERE is_core = TRUE;
