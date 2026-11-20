
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chats'
      AND column_name = 'history'
  ) THEN
    ALTER TABLE chats DROP COLUMN history;
  END IF;
END $$;
