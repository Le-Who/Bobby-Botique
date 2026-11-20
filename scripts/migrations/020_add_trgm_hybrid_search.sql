
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_ltm_content_trgm
  ON long_term_memory USING gin (content gin_trgm_ops);
