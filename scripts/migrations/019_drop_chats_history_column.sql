-- 019_drop_chats_history_column.sql
-- Remove the legacy chats.history column that is no longer used.
--
-- Chat messages are now stored in the active_chat_messages table (since migration 005).
-- The history column was left behind after the migration to the new message storage
-- and was dropped from production via Supabase dashboard. This migration formalizes
-- the removal for any environment that still has the column.

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'chats'
      AND column_name = 'history'
  ) THEN
    ALTER TABLE chats DROP COLUMN history;
  END IF;
END $$;
