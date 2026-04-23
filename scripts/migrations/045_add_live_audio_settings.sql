-- Migration 045: add per-user Gemini Live Audio settings to chats
ALTER TABLE chats ADD COLUMN IF NOT EXISTS live_voice_name TEXT;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS live_thinking_level TEXT;
