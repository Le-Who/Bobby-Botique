-- Migration 026b: Add predicate_embedding to memory_edges for Semantic Edge Deduplication
-- Purpose: Stores the embedding vector for each edge predicate so that consolidation
--          can detect semantically duplicate predicates (e.g. "любит" vs "обожает")
--          and merge them instead of creating redundant graph edges.
-- Idempotency: All statements use IF NOT EXISTS / are safe to re-run.
-- Atomicity: Wrapped in DO $$ block consistent with migration 024 pattern.
--
-- Note on NULL rows: Existing memory_edges rows will have predicate_embedding = NULL.
-- The HNSW index does NOT index NULL values (Postgres behaviour), which is correct —
-- deduplication only fires for newly embedded rows during consolidation runs.

DO $$ BEGIN
    -- Step 1: Add predicate_embedding column if not already present
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

-- Step 2: HNSW index for cosine similarity lookups during dedup phase.
-- m=8 / ef_construction=32 — lightweight because predicate vectors are few and small.
-- WHERE predicate_embedding IS NOT NULL — skips empty rows, keeps index compact.
CREATE INDEX IF NOT EXISTS idx_memory_edges_pred_embed
    ON memory_edges USING hnsw (predicate_embedding halfvec_cosine_ops)
    WITH (m = 8, ef_construction = 32)
    WHERE predicate_embedding IS NOT NULL;
