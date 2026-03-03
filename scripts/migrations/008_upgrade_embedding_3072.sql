-- Migration: Upgrade embedding dimension from 768 → 3072
-- Reason: gemini-embedding-001 defaults to 3072 dims (pre-normalized).
--         Old text-embedding-004 produced 768 dims.
--         HNSW indexes limit vector type to 2000 dims → use halfvec (up to 4000).

-- Clear old 768-dim embeddings
TRUNCATE long_term_memory;

-- Drop HNSW index first (2000-dim limit for vector type)
DROP INDEX IF EXISTS idx_memory_embedding;

-- Switch to halfvec(3072) — supports up to 4000 dims with HNSW
ALTER TABLE long_term_memory ALTER COLUMN embedding TYPE halfvec(3072);

-- Recreate HNSW index with halfvec operator
CREATE INDEX idx_memory_embedding
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
