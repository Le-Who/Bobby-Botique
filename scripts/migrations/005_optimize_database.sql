-- Migration: Database Optimizations (JSONB, Model Limits, RLS Denormalization)
-- Description: Creates active_chat_messages for O(1) inserts, model_configuration for dynamic limits, and owner_user_id for RLS performance.

-- 1. Create Model Configuration Table
CREATE TABLE IF NOT EXISTS model_configuration (
    model_name TEXT PRIMARY KEY,
    daily_limit INTEGER,
    provider TEXT
);

-- Pre-populate known models
INSERT INTO model_configuration (model_name, daily_limit, provider)
VALUES 
    ('gemini-2.5-flash', 250, 'Google'),
    ('gemini-2.5-pro', 100, 'Google'),
    ('gemini-2.5-flash-lite', 1000, 'Google')
ON CONFLICT (model_name) DO UPDATE SET daily_limit = EXCLUDED.daily_limit, provider = EXCLUDED.provider;

-- Enable RLS
ALTER TABLE model_configuration ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS model_configuration_policy ON model_configuration;
CREATE POLICY model_configuration_policy ON model_configuration 
    FOR ALL USING ((select current_setting('app.is_admin', true)) = 'true');


-- 2. Create Active Chat Messages (Hybrid Strategy)
CREATE TABLE IF NOT EXISTS active_chat_messages (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_active_chat_messages_user_id ON active_chat_messages(user_id);
-- Enable RLS
ALTER TABLE active_chat_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS active_chat_messages_policy ON active_chat_messages;
CREATE POLICY active_chat_messages_policy ON active_chat_messages
    FOR ALL USING (
        user_id = NULLIF((select current_setting('app.user_id', true)), '')::bigint OR 
        (select current_setting('app.is_admin', true)) = 'true'
    );


-- 3. RLS Denormalization for conversation_messages and group_messages
-- Add owner_user_id to conversation_messages (linked to conversations table)
ALTER TABLE conversation_messages ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_conversation_messages_owner ON conversation_messages(owner_user_id);

-- Add owner_user_id to group_messages (linked to group_chats)
ALTER TABLE group_messages ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_group_messages_owner ON group_messages(owner_user_id);

-- Update existing data to fill owner_user_id (Denormalization step)
UPDATE conversation_messages cm
SET owner_user_id = c.user_id
FROM conversations c
WHERE cm.conversation_id = c.id AND cm.owner_user_id IS NULL;

-- 4. Update RLS Policies to use owner_user_id (Fixes EXISTS bottleneck)
DROP POLICY IF EXISTS conversation_messages_policy ON conversation_messages;
CREATE POLICY conversation_messages_policy ON conversation_messages FOR ALL USING (
    (select current_setting('app.is_admin', true)) = 'true' OR 
    owner_user_id = NULLIF(current_setting('app.user_id', true), '')::bigint
);

-- Create triggers to auto-fill owner_user_id on INSERT for conversation_messages
CREATE OR REPLACE FUNCTION set_conversation_owner_user_id()
RETURNS TRIGGER AS $$
BEGIN
    SELECT user_id INTO NEW.owner_user_id 
    FROM conversations 
    WHERE id = NEW.conversation_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = public;

DROP TRIGGER IF EXISTS trg_set_conversation_owner ON conversation_messages;
CREATE TRIGGER trg_set_conversation_owner
BEFORE INSERT ON conversation_messages
FOR EACH ROW
EXECUTE FUNCTION set_conversation_owner_user_id();


-- Helper function for ETL JSON unpacking in Postgres (used in save_conversation)
CREATE OR REPLACE PROCEDURE save_chat_to_conversation(
    p_user_id BIGINT,
    p_conv_id INTEGER
)
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    INSERT INTO conversation_messages (conversation_id, role, content, owner_user_id)
    SELECT p_conv_id, role, content, p_user_id
    FROM active_chat_messages
    WHERE user_id = p_user_id
      AND role IN ('user', 'assistant')
      AND content NOT ILIKE '/%'
      AND content NOT ILIKE '%🖼️ обрабатываю изображение%'
      AND content NOT ILIKE '%🤔 думаю%'
      AND content NOT ILIKE '%📄 обрабатываю документ%'
      AND content NOT ILIKE '%✅ новый чат создан%'
      AND content NOT ILIKE '%опишите, какую роль хотите создать%'
      AND content NOT ILIKE '%не удалось сгенерировать роль%'
      AND content NOT ILIKE '%сервер перегружен%';
END;
$$;
