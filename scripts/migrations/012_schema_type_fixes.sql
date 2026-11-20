
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

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'user_documents'
      AND column_name = 'user_id' AND is_nullable = 'YES'
  ) THEN
    ALTER TABLE user_documents ALTER COLUMN user_id SET NOT NULL;
  END IF;
END $$;

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
