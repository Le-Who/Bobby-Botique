-- Migration 028: Add temporal validity to memory_edges
-- Purpose: Enables the Temporal Graph pattern where conflicting facts
--          (e.g. "User — works at — Company A" superseded by "User — works at — Company B")
--          are resolved by closing the old edge (valid_to = now()) and inserting
--          a new one with valid_from = now().  This preserves full history while
--          keeping retrieval queries clean (WHERE valid_to IS NULL).
--
-- Idempotency: All statements use IF NOT EXISTS / conditional guards — safe to re-run.
-- Backward compatibility: Old rows get valid_from = now(), valid_to = NULL (current).

DO $$ BEGIN
    -- Step 1: Add valid_from column
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

    -- Step 2: Add valid_to column
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

-- Step 3: Partial index for fast lookup of only current (non-expired) edges
CREATE INDEX IF NOT EXISTS idx_memory_edges_temporal
    ON memory_edges (user_id, source_node, target_node)
    WHERE valid_to IS NULL;
