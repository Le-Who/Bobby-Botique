-- Migration 024: Upgrade embedding to gemini-embedding-2-preview (768-dim)
-- Reason: gemini-embedding-2-preview supports multimodal & 768 is recommended sweet-spot.
--         Embedding spaces between v1 (3072) and v2 (768) are INCOMPATIBLE.
--         Old data MUST be wiped before ALTER to avoid type mismatch errors.
-- Idempotency: Only truncates+alters if column is NOT already halfvec(768).
--              Safe to run multiple times — no-ops once already at halfvec(768).
-- Handles:
--   1. halfvec(3072) from 008b → truncate + alter to halfvec(768)
--   2. vector(any)   from pre-008b → truncate + alter to halfvec(768)
--   3. halfvec(768)  already migrated → no-op

-- Step 1: Handle legacy 'vector' type (pre-008b installations)
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

-- Step 2: Handle halfvec(3072) from migration 008b
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'long_term_memory'
      AND column_name = 'embedding' AND udt_name = 'halfvec'
  )
  AND NOT EXISTS (
    -- Guard: skip if column is already 768-dim.
    -- atttypmod for halfvec(N) stores N+4 internally in pg_attribute.
    SELECT 1 FROM pg_attribute a
      JOIN pg_class c ON a.attrelid = c.oid
      JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname = 'public'
      AND c.relname = 'long_term_memory'
      AND a.attname = 'embedding'
      AND a.atttypmod = 772  -- 768 + 4
  ) THEN
    TRUNCATE long_term_memory;
    ALTER TABLE long_term_memory ALTER COLUMN embedding TYPE halfvec(768);
  END IF;
END $$;

-- Step 3: Drop old HNSW index (may be built for wrong dimension)
DROP INDEX IF EXISTS idx_memory_embedding;

-- Step 4: Recreate HNSW index with correct dimension (idempotent)
CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
