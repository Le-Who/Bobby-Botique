-- Migration 023: Add branch_id to chats table
-- Allows persisting the active branch across bot restarts.

ALTER TABLE chats ADD COLUMN IF NOT EXISTS branch_id INTEGER REFERENCES conversation_branches(id) ON DELETE SET NULL;

COMMENT ON COLUMN chats.branch_id IS 'Active conversation branch snapshot ID (NULL = main thread)';
