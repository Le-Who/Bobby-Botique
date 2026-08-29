-- Backfill chat preferences that historically existed only in live databases.
-- The runtime repository reads and writes these columns on every chat update,
-- so clean databases must receive them through the numbered migration chain.

ALTER TABLE public.chats ADD COLUMN IF NOT EXISTS thinking_level TEXT;
ALTER TABLE public.chats ADD COLUMN IF NOT EXISTS temperature FLOAT;
ALTER TABLE public.chats ADD COLUMN IF NOT EXISTS voice_id TEXT;
