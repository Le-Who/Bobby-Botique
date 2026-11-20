
ALTER TABLE public.chats ADD COLUMN IF NOT EXISTS thinking_level TEXT;
ALTER TABLE public.chats ADD COLUMN IF NOT EXISTS temperature FLOAT;
ALTER TABLE public.chats ADD COLUMN IF NOT EXISTS voice_id TEXT;
