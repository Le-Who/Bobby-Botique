"""
Initial data seeding — admin user, API keys, and indexes.

Extracted from app/database.py to reduce monolith size.
"""

import hashlib
import logging

_KEY_TABLES = frozenset({"api_keys", "tavily_api_keys", "openrouter_api_keys"})


def _encrypted_key_rows(keys):
    from app.crypto import encrypt_api_key

    rows = []
    seen_hashes = set()
    for key in keys:
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if key_hash in seen_hashes:
            continue
        seen_hashes.add(key_hash)
        rows.append((key_hash, encrypt_api_key(key)))
    return rows


async def _sync_key_table(
    db_query,
    db_execute_many,
    *,
    table_name: str,
    keys,
    prune_key_model_status: bool = False,
) -> None:
    if table_name not in _KEY_TABLES:
        raise ValueError(f"Unsupported key table: {table_name!r}")

    rows = _encrypted_key_rows(keys)
    if not rows:
        logging.info("No %s keys configured; leaving existing rows unchanged.", table_name)
        return

    await db_execute_many(
        f"INSERT INTO {table_name} (key_hash, api_key) "
        "VALUES ($1, $2) "
        "ON CONFLICT (key_hash) DO UPDATE SET api_key = EXCLUDED.api_key",
        rows,
    )

    expected_hashes = [key_hash for key_hash, _ in rows]
    if prune_key_model_status:
        await db_query(
            f"""
            DELETE FROM key_model_status
            WHERE key_hash IN (
                SELECT key_hash
                FROM {table_name}
                WHERE key_hash != ALL($1::text[])
            )
            """,
            (expected_hashes,),
        )

    await db_query(
        f"DELETE FROM {table_name} WHERE key_hash != ALL($1::text[])",
        (expected_hashes,),
    )
    logging.info("Synced %d %s key(s) from config.", len(rows), table_name)


async def insert_initial_data(db_query, db_execute_many, settings):
    """Insert admin user, encrypt+upsert API keys, and create indexes."""
    await db_query(
        "INSERT INTO users (user_id, is_authorized) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING",
        (settings.ADMIN_ID,),
    )

    await _sync_key_table(
        db_query,
        db_execute_many,
        table_name="api_keys",
        keys=settings.GEMINI_API_KEYS,
        prune_key_model_status=True,
    )
    await _sync_key_table(
        db_query,
        db_execute_many,
        table_name="tavily_api_keys",
        keys=settings.TAVILY_API_KEYS,
    )
    await _sync_key_table(
        db_query,
        db_execute_many,
        table_name="openrouter_api_keys",
        keys=settings.OPENROUTER_API_KEYS,
        prune_key_model_status=True,
    )

    await db_query("CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id)")
    await db_query(
        "CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv_id ON conversation_messages(conversation_id)"
    )
    await db_query("CREATE INDEX IF NOT EXISTS idx_key_usage_model_date ON key_usage(model_name, usage_date)")

    # Sync model limits from settings → DB (single source of truth)
    if settings.DAILY_LIMITS:
        limits_data = [(model, limit, "Google") for model, limit in settings.DAILY_LIMITS.items()]
        if limits_data:
            await db_execute_many(
                """INSERT INTO model_configuration (model_name, daily_limit, provider)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (model_name) DO UPDATE
                   SET daily_limit = EXCLUDED.daily_limit, provider = EXCLUDED.provider""",
                limits_data,
            )
            logging.info("Synced %d model limits from config to DB.", len(limits_data))

    logging.info("Initial data seeded.")
