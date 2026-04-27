"""
Chat state management: loading, saving, and history extraction.

Extracted from app/database.py to isolate chat-domain business logic.
The ChatState dataclass remains in app/database.py for backward compatibility
and is re-exported here for convenience.
"""

import logging

from pydantic import ValidationError

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
    """Extract content string from a message dict without massive string allocations."""
    val = msg["content"] if "content" in msg else msg.get("parts", "")
    if isinstance(val, str):
        return val
    if isinstance(val, (bytes, bytearray)):
        return ""
    if isinstance(val, dict):
        return str(val.get("text", "")) if "text" in val else ""
    if isinstance(val, list):
        text_parts = []
        for p in val:
            if isinstance(p, (bytes, bytearray)):
                continue
            if isinstance(p, dict):
                if "text" in p:
                    text_parts.append(str(p["text"]))
            else:
                text_parts.append(str(p))
        return " ".join(text_parts)
    return str(val)


import json


@timed_operation("get_user_chat")
async def get_user_chat(user_id: int) -> ChatState | None:
    """Load the active chat state for a user from the database."""
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, False, conn=conn)
        try:
            query = """
                SELECT
                    (SELECT row_to_json(u)::jsonb FROM (SELECT is_deep_dive, deep_dive_thread_id FROM public.users WHERE user_id = $1) u) as user_info,
                    (SELECT row_to_json(c)::jsonb FROM (SELECT model, token_count, search_enabled, system_prompt, context_summary, thinking_level, ltm_enabled, branch_id, temperature, voice_id, tts_temperature FROM public.chats WHERE user_id = $1) c) as chat_info,
                    (SELECT COALESCE(jsonb_agg(jsonb_build_object('role', role, 'content', content) ORDER BY id ASC), '[]'::jsonb) FROM public.active_chat_messages WHERE user_id = $1) as messages
            """
            result = await db_query(query, (user_id,), conn=conn)

            if not result:
                # Should normally have 1 full row even if empty, but safety check
                return _default_chat_state()

            row = result[0]
            chat_info = row.get("chat_info")
            user_info = row.get("user_info")
            messages = row.get("messages", [])

            # Defensive decode in case asyncpg misses jsonb codec mapping
            if isinstance(chat_info, str):
                chat_info = json.loads(chat_info)
            if isinstance(user_info, str):
                user_info = json.loads(user_info)
            if isinstance(messages, str):
                messages = json.loads(messages)

            if not chat_info:
                chat_state = _default_chat_state()
            else:
                try:
                    from app.core.entities import ChatStateRow

                    validated = ChatStateRow.model_validate(chat_info)
                    history = [
                        {"role": m.get("role", "user"), "parts": [m.get("content", "")]} for m in (messages or [])
                    ]
                    chat_state = ChatState(
                        history=history,
                        model=validated.model or _default_model(),
                        token_count=validated.token_count,
                        search_enabled=validated.search_enabled,
                        system_prompt=validated.system_prompt,
                        context_summary=validated.context_summary,
                        thinking_level=validated.thinking_level,
                        ltm_enabled=validated.ltm_enabled,
                        branch_id=validated.branch_id,
                        temperature=validated.temperature,
                        voice_id=validated.voice_id,
                        tts_temperature=validated.tts_temperature,
                    )
                except ValidationError as ve:
                    logging.warning(
                        "ChatStateRow validation failed for user %s, falling back to .get(): %s",
                        user_id,
                        ve,
                    )
                    history = [
                        {"role": m.get("role", "user"), "parts": [m.get("content", "")]} for m in (messages or [])
                    ]
                    chat_state = ChatState(
                        history=history,
                        model=chat_info.get("model"),
                        token_count=chat_info.get("token_count", 0),
                        search_enabled=chat_info.get("search_enabled", False),
                        system_prompt=chat_info.get("system_prompt"),
                        context_summary=chat_info.get("context_summary"),
                        thinking_level=chat_info.get("thinking_level"),
                        ltm_enabled=chat_info.get("ltm_enabled", True),
                        branch_id=chat_info.get("branch_id"),
                        temperature=chat_info.get("temperature"),
                        voice_id=chat_info.get("voice_id"),
                        tts_temperature=chat_info.get("tts_temperature"),
                    )

            if user_info:
                try:
                    from app.core.entities import UserInfoRow

                    u = UserInfoRow.model_validate(user_info)
                    chat_state.is_deep_dive = u.is_deep_dive
                    chat_state.deep_dive_thread_id = u.deep_dive_thread_id
                except ValidationError:
                    chat_state.is_deep_dive = bool(user_info.get("is_deep_dive", False))
                    chat_state.deep_dive_thread_id = user_info.get("deep_dive_thread_id")

            chat_state._original_length = len(chat_state.history)

            return chat_state
        finally:
            await clear_user_context(conn=conn)


def _default_model() -> str:
    return settings.DEFAULT_MODEL if settings else "gemini-3.1-flash-lite-preview"


def _default_chat_state() -> ChatState:
    return ChatState(
        history=[],
        model=_default_model(),
        token_count=0,
        search_enabled=False,
        system_prompt=None,
    )


@timed_operation("update_user_chat")
async def update_user_chat(user_id: int, chat_state: ChatState) -> None:
    """Save the chat state back to the database, syncing new messages."""
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, False, conn=conn)
        try:
            current_length = len(chat_state.history)
            original_length = getattr(chat_state, "_original_length", 0)

            should_delete = False
            messages_to_insert = []

            if current_length == 0 and original_length > 0:
                should_delete = True
            elif current_length > original_length:
                new_msgs = chat_state.history[original_length:]
                for msg in new_msgs:
                    messages_to_insert.append(
                        {
                            "role": msg.get("role", "user"),
                            "content": _extract_message_content(msg),
                        }
                    )
            elif current_length < original_length:
                should_delete = True
                for msg in chat_state.history:
                    messages_to_insert.append(
                        {
                            "role": msg.get("role", "user"),
                            "content": _extract_message_content(msg),
                        }
                    )

            messages_json = json.dumps(messages_to_insert) if messages_to_insert else "[]"
            chat_state._original_length = current_length

            query = """
            WITH update_chats AS (
                INSERT INTO public.chats (user_id, model, token_count, search_enabled, system_prompt, context_summary, thinking_level, ltm_enabled, branch_id, temperature, voice_id, tts_temperature)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $13, $14, $15, $16)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    model = EXCLUDED.model, token_count = EXCLUDED.token_count,
                    search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt,
                    context_summary = EXCLUDED.context_summary, thinking_level = EXCLUDED.thinking_level,
                    ltm_enabled = EXCLUDED.ltm_enabled, branch_id = EXCLUDED.branch_id,
                    temperature = EXCLUDED.temperature, voice_id = EXCLUDED.voice_id, tts_temperature = EXCLUDED.tts_temperature
            ),
            update_users AS (
                UPDATE public.users SET is_deep_dive = $9, deep_dive_thread_id = $10 WHERE user_id = $1
            ),
            delete_messages AS (
                DELETE FROM public.active_chat_messages WHERE user_id = $1 AND $11
            )
            INSERT INTO public.active_chat_messages (user_id, role, content)
            SELECT $1, role, content FROM json_to_recordset($12::json) AS x(role text, content text)
            WHERE $12::json IS NOT NULL AND json_array_length($12::json) > 0;
            """

            await db_query(
                query,
                (
                    user_id,
                    chat_state.model,
                    chat_state.token_count,
                    chat_state.search_enabled,
                    chat_state.system_prompt,
                    chat_state.context_summary,
                    chat_state.thinking_level,
                    chat_state.ltm_enabled,
                    chat_state.is_deep_dive,
                    chat_state.deep_dive_thread_id,
                    should_delete,
                    messages_json,
                    chat_state.branch_id,
                    chat_state.temperature,
                    chat_state.voice_id,
                    chat_state.tts_temperature,
                ),
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
        FROM public.chats
        WHERE model IS NOT NULL
        AND model NOT IN ({placeholders})
        """,
        tuple(available_models),
    )

    migrated = 0
    gemini_users = []
    openrouter_users = []
    for chat in invalid_chats:
        user_id = chat["user_id"]
        old_model = chat["model"]
        if "/" in old_model:
            openrouter_users.append(user_id)
        else:
            gemini_users.append(user_id)
        logging.info("Migrating user %s from %s", user_id, old_model)

    if gemini_users:
        await db_query(
            "UPDATE public.chats SET model = $1 WHERE user_id = ANY($2)",
            (default_gemini_model, gemini_users),
        )
        migrated += len(gemini_users)

    if openrouter_users:
        await db_query(
            "UPDATE public.chats SET model = $1 WHERE user_id = ANY($2)",
            (default_openrouter_model, openrouter_users),
        )
        migrated += len(openrouter_users)

    if migrated:
        logging.warning("Migrated %d users to default models after config reload", migrated)
    return migrated


async def model_migration_watcher(old_settings, new_settings) -> None:
    """Config watcher: migrates users whose model is no longer available.

    Registered via ``config_manager.add_watcher()`` at startup so that config.py
    never imports from the DB/repos layer directly (AR-4).
    """
    try:
        all_available = set()
        if new_settings.AVAILABLE_MODELS:
            all_available.update(new_settings.AVAILABLE_MODELS)
        if new_settings.OPENROUTER_AVAILABLE_MODELS:
            all_available.update(new_settings.OPENROUTER_AVAILABLE_MODELS)

        await migrate_invalid_models(
            available_models=all_available,
            default_gemini_model=new_settings.DEFAULT_MODEL,
            default_openrouter_model=new_settings.OPENROUTER_DEFAULT_MODEL,
        )
    except Exception as e:
        logging.error("Model migration watcher error: %s", e)


async def update_thinking_level(user_id: int, level: str | None) -> None:
    """Update thinking level for a user's chat. None resets to default."""
    await db_query(
        "UPDATE public.chats SET thinking_level = $1 WHERE user_id = $2",
        (level, user_id),
    )
