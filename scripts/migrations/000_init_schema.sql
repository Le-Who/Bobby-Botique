-- 000_init_schema.sql
-- Initial schema definition from legacy Python code.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY, 
    is_authorized INTEGER DEFAULT 0,
    is_deep_dive BOOLEAN DEFAULT FALSE,
    deep_dive_thread_id TEXT
);

CREATE TABLE IF NOT EXISTS chats (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE, 
    model TEXT, 
    token_count INTEGER DEFAULT 0, 
    search_enabled BOOLEAN DEFAULT FALSE, 
    system_prompt TEXT,
    context_summary TEXT
);

CREATE TABLE IF NOT EXISTS roles (
    id SERIAL PRIMARY KEY,
    key TEXT UNIQUE,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_roles (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    role_type TEXT NULL,
    role_id INTEGER NULL,
    summary TEXT NULL,
    token_budget BIGINT NULL,
    archived BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    token_estimate BIGINT DEFAULT 0,
    owner_user_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash TEXT PRIMARY KEY, 
    api_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS key_usage (
    key_hash TEXT REFERENCES api_keys(key_hash) ON DELETE CASCADE, 
    model_name TEXT, 
    usage_date DATE, 
    request_count INTEGER DEFAULT 0, 
    PRIMARY KEY (key_hash, model_name, usage_date)
);

CREATE TABLE IF NOT EXISTS metrics (
    id SERIAL PRIMARY KEY,
    metric_date DATE NOT NULL,
    request_count INTEGER DEFAULT 0,
    total_response_time REAL DEFAULT 0.0,
    error_count INTEGER DEFAULT 0,
    search_queries INTEGER DEFAULT 0,
    cache_hits INTEGER DEFAULT 0,
    cache_misses INTEGER DEFAULT 0,
    api_calls JSONB DEFAULT '{}'::jsonb,
    model_usage JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(metric_date)
);

CREATE TABLE IF NOT EXISTS error_logs (
    id SERIAL PRIMARY KEY,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    request_id TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tavily_api_keys (
    key_hash TEXT PRIMARY KEY, 
    api_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tavily_key_usage (
    key_hash TEXT REFERENCES tavily_api_keys(key_hash) ON DELETE CASCADE, 
    usage_month TEXT, 
    credit_usage INTEGER DEFAULT 0, 
    PRIMARY KEY (key_hash, usage_month)
);

CREATE TABLE IF NOT EXISTS openrouter_api_keys (
    key_hash TEXT PRIMARY KEY, 
    api_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS openrouter_key_usage (
    key_hash TEXT REFERENCES openrouter_api_keys(key_hash) ON DELETE CASCADE, 
    model_name TEXT, 
    usage_date DATE, 
    request_count INTEGER DEFAULT 0, 
    PRIMARY KEY (key_hash, model_name, usage_date)
);

CREATE TABLE IF NOT EXISTS key_model_status (
    key_hash       TEXT NOT NULL,
    model_name     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    suspended_until TIMESTAMPTZ,
    failure_count  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (key_hash, model_name)
);

CREATE TABLE IF NOT EXISTS user_documents (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    filename TEXT,
    content TEXT,
    pages INTEGER,
    file_size BIGINT,
    file_hash TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, file_hash)
);

CREATE TABLE IF NOT EXISTS user_state (
    user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    document_mode BOOLEAN DEFAULT FALSE,
    selected_document_id INTEGER,
    awaiting_custom_role_input BOOLEAN DEFAULT FALSE,
    generated_role JSONB,
    last_custom_role_prompt TEXT,
    generating_custom_role BOOLEAN DEFAULT FALSE,
    last_sent_message_text TEXT,
    awaiting_manual_role_title BOOLEAN DEFAULT FALSE,
    awaiting_manual_role_prompt BOOLEAN DEFAULT FALSE,
    manual_role_title TEXT DEFAULT '',
    manual_role_prompt TEXT DEFAULT '',
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    message_id BIGINT,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Per-user daily metrics (added by migration 003, included here for completeness)
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

-- Dynamic model limits (added by migration 005)
CREATE TABLE IF NOT EXISTS model_configuration (
    model_name TEXT PRIMARY KEY,
    daily_limit INTEGER,
    provider TEXT
);

-- Active chat messages for O(1) inserts (added by migration 005)
CREATE TABLE IF NOT EXISTS active_chat_messages (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Long-term memory with pgvector embeddings (created via Supabase dashboard, never had a migration)
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

-- Group chat tables (previously created at runtime in group_chat.py)
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
