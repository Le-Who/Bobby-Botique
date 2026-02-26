"""
Database schema definitions — all CREATE TABLE IF NOT EXISTS statements.

Extracted from app/database.py to reduce monolith size.
Called once on startup via init_db().
"""

import logging


async def create_tables(db_query):
    """Create all application tables if they don't exist."""

    await db_query(
        "CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, is_authorized INTEGER DEFAULT 0, is_deep_dive BOOLEAN DEFAULT FALSE)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS chats (user_id BIGINT PRIMARY KEY, history TEXT, model TEXT, token_count INTEGER DEFAULT 0, search_enabled INTEGER DEFAULT 0, system_prompt TEXT)"
    )

    await db_query("""
        CREATE TABLE IF NOT EXISTS roles (
            id SERIAL PRIMARY KEY,
            key TEXT UNIQUE,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            is_default BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS user_roles (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            title TEXT NOT NULL,
            role_type TEXT NULL,
            role_id INTEGER NULL,
            summary TEXT NULL,
            token_budget BIGINT NULL,
            archived BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            token_estimate BIGINT DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db_query(
        "CREATE TABLE IF NOT EXISTS api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))"
    )
    await db_query("""
        CREATE TABLE IF NOT EXISTS metrics (
            id SERIAL PRIMARY KEY,
            metric_date DATE NOT NULL,
            request_count INTEGER DEFAULT 0,
            total_response_time REAL DEFAULT 0.0,
            error_count INTEGER DEFAULT 0,
            search_queries INTEGER DEFAULT 0,
            cache_hits INTEGER DEFAULT 0,
            cache_misses INTEGER DEFAULT 0,
            api_calls JSONB DEFAULT '{}',
            model_usage JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(metric_date)
        )
    """)
    await db_query("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id SERIAL PRIMARY KEY,
            error_type TEXT NOT NULL,
            error_message TEXT NOT NULL,
            request_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db_query(
        "CREATE TABLE IF NOT EXISTS tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER DEFAULT 0, PRIMARY KEY (key_hash, usage_month))"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS openrouter_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)"
    )
    await db_query(
        "CREATE TABLE IF NOT EXISTS openrouter_key_usage (key_hash TEXT, model_name TEXT, usage_date DATE, request_count INTEGER DEFAULT 0, PRIMARY KEY (key_hash, model_name, usage_date))"
    )

    await db_query("""
        CREATE TABLE IF NOT EXISTS user_documents (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            filename TEXT,
            content TEXT,
            pages INTEGER,
            file_size BIGINT,
            file_hash TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, file_hash)
        )
    """)

    await db_query("""
        CREATE TABLE IF NOT EXISTS user_state (
            user_id BIGINT PRIMARY KEY,
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
        )
    """)

    await db_query("""
        CREATE TABLE IF NOT EXISTS feedback (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            message_id BIGINT,
            rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    logging.info("Database schema initialized.")
