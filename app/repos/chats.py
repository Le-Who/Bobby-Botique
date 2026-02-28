"""
Chat state management: loading, saving, and history extraction.

Extracted from app/database.py to isolate chat-domain business logic.
The ChatState dataclass remains in app/database.py for backward compatibility
and is re-exported here for convenience.
"""

import logging

from cachetools import TTLCache

from app.config import settings
from app.database import (
    ChatState,
    clear_user_context,
    db_execute_many,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)
from app.utils.logging_config import timed_operation


def _extract_message_content(msg: dict) -> str:
    """Extract content string from a message dict that may use 'content' or 'parts' key."""
    if "content" in msg:
        content = msg["content"]
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(str(p) for p in content)
        return str(content)
    if "parts" in msg:
        parts = msg["parts"]
        if isinstance(parts, list):
            return " ".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in parts)
        return str(parts)
    return ""


@timed_operation("get_user_chat")
async def get_user_chat(user_id: int) -> ChatState | None:
    """Load the active chat state for a user from the database."""
    # Check cache first
    async with db_manager._cache_lock:
        if (
            hasattr(db_manager, "_active_chats_cache")
            and user_id in db_manager._active_chats_cache
        ):
            return db_manager._active_chats_cache[user_id]

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, False, conn=conn)
        try:
            chat_result = await db_query(
                "SELECT model, token_count, search_enabled, system_prompt, context_summary FROM chats WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            user_result = await db_query(
                "SELECT is_deep_dive, deep_dive_thread_id FROM users WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )

            if not chat_result:
                default_model = settings.DEFAULT_MODEL if settings else "gemini-2.0-flash"
                chat_state = ChatState(
                    history=[],
                    model=default_model,
                    token_count=0,
                    search_enabled=False,
                    system_prompt=None,
                )
            else:
                row = chat_result[0]
                messages = await db_query(
                    "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC",
                    (user_id,),
                    conn=conn,
                )
                history = [{"role": m["role"], "parts": [m["content"]]} for m in messages]

                chat_state = ChatState(
                    history=history,
                    model=row["model"],
                    token_count=row["token_count"],
                    search_enabled=row["search_enabled"],
                    system_prompt=row["system_prompt"],
                    context_summary=row.get("context_summary"),
                )

            if user_result:
                chat_state.is_deep_dive = bool(user_result[0].get("is_deep_dive", False))
                chat_state.deep_dive_thread_id = user_result[0].get("deep_dive_thread_id")

            chat_state._original_length = len(chat_state.history)

            # Update cache
            async with db_manager._cache_lock:
                if not hasattr(db_manager, "_active_chats_cache"):
                    db_manager._active_chats_cache = TTLCache(maxsize=1000, ttl=900)
                db_manager._active_chats_cache[user_id] = chat_state

            return chat_state
        finally:
            await clear_user_context(conn=conn)


@timed_operation("update_user_chat")
async def update_user_chat(user_id: int, chat_state: ChatState) -> None:
    """Save the chat state back to the database, syncing new messages."""
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, False, conn=conn)
        try:
            # Update cache
            async with db_manager._cache_lock:
                if not hasattr(db_manager, "_active_chats_cache"):
                    db_manager._active_chats_cache = TTLCache(maxsize=1000, ttl=900)
                db_manager._active_chats_cache[user_id] = chat_state

            current_length = len(chat_state.history)
            original_length = getattr(chat_state, "_original_length", 0)

            if current_length == 0 and original_length > 0:
                # History was cleared — delete all messages
                await db_query(
                    "DELETE FROM active_chat_messages WHERE user_id = $1",
                    (user_id,),
                    conn=conn,
                )
            elif current_length > original_length:
                # New messages appended — insert only the new ones
                new_msgs = chat_state.history[original_length:]

                insert_data = []
                for msg in new_msgs:
                    role = msg.get("role", "user")
                    content = _extract_message_content(msg)
                    insert_data.append((user_id, role, content))

                if insert_data:
                    await db_execute_many(
                        "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
                        insert_data,
                        conn=conn,
                    )
            elif current_length < original_length:
                # History was trimmed — full rewrite
                await db_query(
                    "DELETE FROM active_chat_messages WHERE user_id = $1",
                    (user_id,),
                    conn=conn,
                )

                insert_data = []
                for msg in chat_state.history:
                    role = msg.get("role", "user")
                    content = _extract_message_content(msg)
                    insert_data.append((user_id, role, content))
                if insert_data:
                    await db_execute_many(
                        "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
                        insert_data,
                        conn=conn,
                    )

            chat_state._original_length = current_length

            chat_query = """
            INSERT INTO chats (user_id, history, model, token_count, search_enabled, system_prompt, context_summary)
            VALUES ($1, '[]', $2, $3, $4, $5, $6)
            ON CONFLICT (user_id)
            DO UPDATE SET
                model = EXCLUDED.model, token_count = EXCLUDED.token_count,
                search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt,
                context_summary = EXCLUDED.context_summary;
            """
            await db_query(
                chat_query,
                (
                    user_id,
                    chat_state.model,
                    chat_state.token_count,
                    chat_state.search_enabled,
                    chat_state.system_prompt,
                    chat_state.context_summary,
                ),
                conn=conn,
            )

            user_query = "UPDATE users SET is_deep_dive = $1, deep_dive_thread_id = $2 WHERE user_id = $3"
            await db_query(
                user_query,
                (chat_state.is_deep_dive, chat_state.deep_dive_thread_id, user_id),
                conn=conn,
            )
        finally:
            await clear_user_context(conn=conn)


async def migrate_invalid_models(
    available_models: set,
    default_gemini_model: str,
    default_openrouter_model: str,
) -> int:
    """Migrate users whose active model is no longer in the available set.

    Returns the number of migrated users.
    """
    if not available_models or not db_manager.is_connected:
        return 0

    placeholders = ",".join([f"${i + 1}" for i in range(len(available_models))])
    invalid_chats = await db_query(
        f"""
        SELECT user_id, model
        FROM chats
        WHERE model IS NOT NULL
        AND model NOT IN ({placeholders})
        """,
        tuple(available_models),
    )

    migrated = 0
    for chat in invalid_chats:
        user_id = chat["user_id"]
        old_model = chat["model"]
        target = default_openrouter_model if "/" in old_model else default_gemini_model
        await db_query(
            "UPDATE chats SET model = $1 WHERE user_id = $2",
            (target, user_id),
        )
        migrated += 1
        logging.info("Migrated user %s from %s to %s", user_id, old_model, target)

    if migrated:
        logging.warning("Migrated %d users to default models after config reload", migrated)
    return migrated
