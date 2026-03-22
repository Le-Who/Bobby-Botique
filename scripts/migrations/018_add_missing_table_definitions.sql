-- 018_add_missing_table_definitions.sql
-- Backfill: create tables that were originally created outside the migration system.
--
-- For EXISTING databases (where 000_init_schema was already applied without these tables),
-- this migration ensures all tables exist. All statements use IF NOT EXISTS for idempotency.

-- 1. Per-user daily metrics (originally migration 003, but not in 000_init_schema)
CREATE TABLE IF NOT EXISTS user_metrics (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    metric_date DATE NOT NULL DEFAULT CURRENT_DATE,
    request_count INT DEFAULT 0,
    model_usage JSONB DEFAULT '{}',
    current_streak INT DEFAULT 0,
    longest_streak INT DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, metric_date)
);

CREATE INDEX IF NOT EXISTS idx_user_metrics_user_date ON user_metrics (user_id, metric_date);

-- 2. Dynamic model limits (originally migration 005, but not in 000_init_schema)
CREATE TABLE IF NOT EXISTS model_configuration (
    model_name TEXT PRIMARY KEY,
    daily_limit INTEGER,
    provider TEXT
);

-- 3. Active chat messages (originally migration 005, but not in 000_init_schema)
CREATE TABLE IF NOT EXISTS active_chat_messages (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_active_chat_messages_user_id ON active_chat_messages(user_id);

-- 4. Long-term memory (was created via Supabase dashboard, never had a migration)
CREATE TABLE IF NOT EXISTS long_term_memory (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding halfvec(3072),
    source_type TEXT NOT NULL DEFAULT 'conversation',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

-- HNSW index for vector similarity search (idempotent)
CREATE INDEX IF NOT EXISTS idx_memory_embedding
    ON long_term_memory USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 5. Group chat tables (previously created at runtime in group_chat.py)
CREATE TABLE IF NOT EXISTS group_chats (
    chat_id BIGINT PRIMARY KEY,
    title TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    member_count INTEGER DEFAULT 0,
    admin_user_id BIGINT NOT NULL,
    settings JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS group_members (
    chat_id BIGINT REFERENCES group_chats(chat_id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
    joined_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    is_admin BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS group_messages (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL REFERENCES group_chats(chat_id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    message_text TEXT,
    message_type TEXT DEFAULT 'text',
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    is_bot_response BOOLEAN DEFAULT FALSE,
    owner_user_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL
);
