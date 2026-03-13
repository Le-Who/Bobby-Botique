-- Migration: Upgrade embedding dimension from 768 → 3072
-- Reason: gemini-embedding-001 defaults to 3072 dims (pre-normalized).
--         Old text-embedding-004 produced 768 dims.
--         HNSW indexes limit vector type to 2000 dims → use halfvec (up to 4000).

-- Clear old 768-dim embeddings (only if column is still the old type)
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'long_term_memory'
      AND column_name = 'embedding' AND udt_name = 'vector'
  ) THEN
    TRUNCATE long_term_memory;
    ALTER TABLE long_term_memory ALTER COLUMN embedding TYPE halfvec(3072);
  END IF;
END $$;

-- Drop old HNSW index (may be wrong type)
DROP INDEX IF EXISTS idx_memory_embedding;

-- Recreate HNSW index with halfvec operator (idempotent)
CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
