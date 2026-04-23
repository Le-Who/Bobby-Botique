-- Migration 046: add per-user live transport preset to chats
ALTER TABLE chats ADD COLUMN IF NOT EXISTS live_connection_mode TEXT;
