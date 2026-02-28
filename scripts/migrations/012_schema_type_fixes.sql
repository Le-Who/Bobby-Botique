-- Migration 012: Schema type fixes (M2-M5)
-- Applied via Supabase MCP on 2026-02-28
--
-- M2: chats.search_enabled INTEGER → BOOLEAN
-- M3: metrics/error_logs timestamps → TIMESTAMPTZ
-- M4: user_documents.user_id → NOT NULL
-- M5: chats.history TEXT → JSONB
--
-- All operations are wrapped in type-checks for idempotency
-- (safe to re-run if already applied via MCP)

-- M2: chats.search_enabled → BOOLEAN (only if still integer)
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chats'
      AND column_name = 'search_enabled' AND data_type = 'integer'
  ) THEN
    ALTER TABLE chats
      ALTER COLUMN search_enabled DROP DEFAULT,
      ALTER COLUMN search_enabled TYPE BOOLEAN USING (search_enabled != 0),
      ALTER COLUMN search_enabled SET DEFAULT FALSE;
  END IF;
END $$;

-- M3: metrics timestamps → TIMESTAMPTZ (only if still timestamp without tz)
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'metrics'
      AND column_name = 'created_at' AND data_type = 'timestamp without time zone'
  ) THEN
    ALTER TABLE metrics
      ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC',
      ALTER COLUMN updated_at TYPE TIMESTAMPTZ USING updated_at AT TIME ZONE 'UTC';
  END IF;
END $$;

-- M3: error_logs.created_at → TIMESTAMPTZ
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'error_logs'
      AND column_name = 'created_at' AND data_type = 'timestamp without time zone'
  ) THEN
    ALTER TABLE error_logs
      ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at AT TIME ZONE 'UTC';
  END IF;
END $$;

-- M4: user_documents.user_id → NOT NULL (only if still nullable)
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'user_documents'
      AND column_name = 'user_id' AND is_nullable = 'YES'
  ) THEN
    ALTER TABLE user_documents ALTER COLUMN user_id SET NOT NULL;
  END IF;
END $$;

-- M5: chats.history → JSONB (only if still text)
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chats'
      AND column_name = 'history' AND data_type = 'text'
  ) THEN
    ALTER TABLE chats
      ALTER COLUMN history TYPE JSONB USING COALESCE(history::jsonb, '[]'::jsonb),
      ALTER COLUMN history SET DEFAULT '[]'::jsonb;
  END IF;
END $$;
