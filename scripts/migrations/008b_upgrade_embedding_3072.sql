
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

DROP INDEX IF EXISTS idx_memory_embedding;

CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
