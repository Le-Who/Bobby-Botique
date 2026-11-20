
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'long_term_memory'
      AND column_name = 'embedding' AND udt_name = 'vector'
  ) THEN
    TRUNCATE long_term_memory;
    ALTER TABLE long_term_memory ALTER COLUMN embedding TYPE halfvec(768);
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'long_term_memory'
      AND column_name = 'embedding' AND udt_name = 'halfvec'
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_attribute a
      JOIN pg_class c ON a.attrelid = c.oid
      JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'public'
      AND c.relname = 'long_term_memory'
      AND a.attname = 'embedding'
      AND a.atttypmod = 772
  ) THEN
    TRUNCATE long_term_memory;
    ALTER TABLE long_term_memory ALTER COLUMN embedding TYPE halfvec(768);
  END IF;
END $$;

DROP INDEX IF EXISTS idx_memory_embedding;

CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
