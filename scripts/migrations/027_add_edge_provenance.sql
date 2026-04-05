-- Migration 027: Add source_memory_ids provenance to memory_edges
-- Purpose: Links graph edges back to the raw long_term_memory rows that
--          originated them.  This enables the HippoRAG 2 "Dual-Node"
--          retrieval pattern: when a 2-hop graph edge is highly relevant,
--          the retriever can also surface the full unstructured passage
--          that the fact was originally extracted from, drastically
--          reducing LLM hallucination around short predicates.
--
-- Idempotency: Uses IF NOT EXISTS guard — safe to re-run.
-- Backward compatibility: Old rows will have source_memory_ids = '{}' (empty).
--          All existing SQL queries are unaffected because the column
--          has a DEFAULT and is not referenced in WHERE/JOIN clauses.

DO $$ BEGIN
    -- Step 1: Add source_memory_ids column if not present
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

-- Step 2: GIN index for array containment lookups (@> operator).
-- This supports queries like "find all edges sourced from memory_id=42".
CREATE INDEX IF NOT EXISTS idx_memory_edges_source_ids
    ON memory_edges USING gin (source_memory_ids)
    WHERE source_memory_ids != '{}';
