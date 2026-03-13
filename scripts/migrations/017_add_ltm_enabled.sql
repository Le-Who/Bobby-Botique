-- Add ltm_enabled column to chats table (long-term memory toggle)
ALTER TABLE chats ADD COLUMN IF NOT EXISTS ltm_enabled BOOLEAN DEFAULT TRUE;
