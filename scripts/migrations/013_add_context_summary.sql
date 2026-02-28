-- Migration 013: Add context_summary column to chats table
-- Persists LLM-generated conversation summaries across bot restarts.

ALTER TABLE chats ADD COLUMN IF NOT EXISTS context_summary TEXT;

COMMENT ON COLUMN chats.context_summary IS 'LLM-generated conversation summary for context compression';
