-- Migration: Upgrade embedding dimension from 768 → 3072
-- Reason: gemini-embedding-001 defaults to 3072 dims (pre-normalized).
--         Old text-embedding-004 produced 768 dims.
--         Incompatible dimensions cannot coexist in same vector column.

-- Clear old 768-dim embeddings (they cannot be mixed with 3072-dim)
TRUNCATE long_term_memory;

-- Resize the vector column to 3072
ALTER TABLE long_term_memory ALTER COLUMN embedding TYPE vector(3072);
