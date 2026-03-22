-- Migration 020: Add pg_trgm extension and GIN index for hybrid memory search
-- Enables keyword matching alongside pgvector cosine similarity (RRF hybrid search).
-- pg_trgm is safe to CREATE IF NOT EXISTS; GIN index uses gin_trgm_ops for % operator.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_ltm_content_trgm
  ON long_term_memory USING gin (content gin_trgm_ops);
