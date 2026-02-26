"""
Initial data seeding — admin user, API keys, and indexes.

Extracted from app/database.py to reduce monolith size.
"""

import hashlib
import logging


async def insert_initial_data(db_query, db_execute_many, settings):
    """Insert admin user, encrypt+upsert API keys, and create indexes."""
    await db_query(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING",
        (settings.ADMIN_ID,),
    )

    from app.crypto import encrypt_api_key

    gemini_data = [
        (hashlib.sha256(key.encode()).hexdigest(), encrypt_api_key(key))
        for key in settings.GEMINI_API_KEYS
    ]
    if gemini_data:
        await db_execute_many(
            "INSERT INTO api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
            gemini_data,
        )

    tavily_data = [
        (hashlib.sha256(key.encode()).hexdigest(), encrypt_api_key(key))
        for key in settings.TAVILY_API_KEYS
    ]
    if tavily_data:
        await db_execute_many(
            "INSERT INTO tavily_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
            tavily_data,
        )

    openrouter_data = [
        (hashlib.sha256(key.encode()).hexdigest(), encrypt_api_key(key))
        for key in settings.OPENROUTER_API_KEYS
    ]
    if openrouter_data:
        await db_execute_many(
            "INSERT INTO openrouter_api_keys (key_hash, api_key) VALUES ($1, $2) ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
            openrouter_data,
        )

    await db_query("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
    await db_query(
        "CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv_id ON conversation_messages(conversation_id)"
    )
    await db_query(
        "CREATE INDEX IF NOT EXISTS idx_key_usage_model_date ON key_usage(model_name, usage_date)"
    )

    # Sync model limits from settings → DB (single source of truth)
    if settings.DAILY_LIMITS:
        limits_data = [
            (model, limit, "Google")
            for model, limit in settings.DAILY_LIMITS.items()
        ]
        if limits_data:
            await db_execute_many(
                """INSERT INTO model_configuration (model_name, daily_limit, provider)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (model_name) DO UPDATE
                   SET daily_limit = EXCLUDED.daily_limit, provider = EXCLUDED.provider""",
                limits_data,
            )
            logging.info(
                "Synced %d model limits from config to DB.", len(limits_data)
            )

    logging.info("Initial data seeded.")
