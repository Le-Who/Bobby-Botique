
ALTER TABLE chats ADD COLUMN IF NOT EXISTS context_summary TEXT;

COMMENT ON COLUMN chats.context_summary IS 'LLM-generated conversation summary for context compression';
