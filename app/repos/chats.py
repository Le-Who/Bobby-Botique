"""
Chat state management: loading, saving, and history extraction.

Extracted from app/database.py to isolate chat-domain business logic.
The ChatState dataclass remains in app/database.py for backward compatibility
and is re-exported here for convenience.
"""

import logging

from pydantic import ValidationError

from app.config import settings

# Performance: Import Pydantic models at module level to avoid __import__ lock overhead in hot DB paths
from app.core.entities import ChatStateRow, UserInfoRow
from app.database import (
    ChatState,
    clear_user_context,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)
from app.providers.base import is_freetheai_model, is_opencode_model, is_openrouter_model
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


from app.utils.json_compat import json


@timed_operation("get_user_chat")
async def get_user_chat(user_id: int) -> ChatState | None:
    """Load the active chat state for a user from the database."""
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            query = """
                SELECT
                    u.is_deep_dive,
                    u.deep_dive_thread_id,
                    c.model,
                    c.token_count,
                    c.search_enabled,
                    c.system_prompt,
                    c.context_summary,
                    c.thinking_level,
                    c.ltm_enabled,
                    c.memory_epoch,
                    c.private_data_blocked,
                    c.branch_id,
                    c.temperature,
                    c.voice_id,
                    c.tts_temperature,
                    c.live_voice_name,
                    c.live_thinking_level,
                    c.live_connection_mode,
                    COALESCE(
                        (SELECT jsonb_agg(jsonb_build_object('role', role, 'content', content) ORDER BY id ASC)
                         FROM public.active_chat_messages
                         WHERE user_id = u.user_id),
                        '[]'::jsonb
                    ) as messages
                FROM public.users u
                LEFT JOIN public.chats c ON u.user_id = c.user_id
                WHERE u.user_id = $1
            """
            result = await db_query(query, (user_id,), conn=conn)

            if not result:
                # User does not exist at all
                return _default_chat_state()

            row = result[0]
            # asyncpg.Record supports mapping interface, cast to dict for safe handling
            row_dict = dict(row)

            messages = row_dict.get("messages", [])
            # Defensive decode in case asyncpg misses jsonb codec mapping
            if isinstance(messages, str):
                messages = json.loads(messages)

            history = [{"role": m.get("role", "user"), "parts": [m.get("content", "")]} for m in (messages or [])]

            # If c.token_count is None, the LEFT JOIN to public.chats found no row
            has_chat = row_dict.get("token_count") is not None

            if not has_chat:
                chat_state = _default_chat_state()
            else:
                try:
                    validated = ChatStateRow.model_validate(row_dict)
                    chat_state = ChatState(
                        history=history,
                        model=validated.model or _default_model(),
                        token_count=validated.token_count,
                        search_enabled=validated.search_enabled,
                        system_prompt=validated.system_prompt,
                        context_summary=validated.context_summary,
                        thinking_level=validated.thinking_level,
                        ltm_enabled=validated.ltm_enabled,
                        memory_epoch=validated.memory_epoch,
                        private_data_blocked=validated.private_data_blocked,
                        branch_id=validated.branch_id,
                        temperature=validated.temperature,
                        voice_id=validated.voice_id,
                        tts_temperature=validated.tts_temperature,
                        live_voice_name=validated.live_voice_name,
                        live_thinking_level=validated.live_thinking_level,
                        live_connection_mode=validated.live_connection_mode,
                    )
                except ValidationError as ve:
                    logging.warning(
                        "ChatStateRow validation failed for user %s, falling back to .get(): %s",
                        user_id,
                        ve,
                    )
                    chat_state = ChatState(
                        history=history,
                        model=row_dict.get("model") or _default_model(),
                        token_count=row_dict.get("token_count", 0),
                        search_enabled=row_dict.get("search_enabled", False),
                        system_prompt=row_dict.get("system_prompt"),
                        context_summary=row_dict.get("context_summary"),
                        thinking_level=row_dict.get("thinking_level"),
                        ltm_enabled=row_dict.get("ltm_enabled", True),
                        memory_epoch=row_dict.get("memory_epoch", 0),
                        private_data_blocked=row_dict.get("private_data_blocked", False),
                        branch_id=row_dict.get("branch_id"),
                        temperature=row_dict.get("temperature"),
                        voice_id=row_dict.get("voice_id"),
                        tts_temperature=row_dict.get("tts_temperature"),
                        live_voice_name=row_dict.get("live_voice_name"),
                        live_thinking_level=row_dict.get("live_thinking_level"),
                        live_connection_mode=row_dict.get("live_connection_mode"),
                    )

            try:
                u = UserInfoRow.model_validate(row_dict)
                chat_state.is_deep_dive = u.is_deep_dive
                chat_state.deep_dive_thread_id = u.deep_dive_thread_id
            except ValidationError:
                chat_state.is_deep_dive = bool(row_dict.get("is_deep_dive", False))
                chat_state.deep_dive_thread_id = row_dict.get("deep_dive_thread_id")

            chat_state._original_length = len(chat_state.history)
            # A default ChatState alone cannot distinguish a genuinely missing
            # row from a legacy row whose valid generation is zero.  Request
            # handlers use this marker to create a generation only for the
            # former, while exact-matching every already persisted snapshot.
            chat_state._has_persisted_chat = has_chat

            return chat_state
        finally:
            await clear_user_context(conn=conn)


def _default_model() -> str:
    return settings.DEFAULT_MODEL if settings else "gemini-3.1-flash-lite"


def _default_chat_state() -> ChatState:
    return ChatState(
        history=[],
        model=_default_model(),
        token_count=0,
        search_enabled=False,
        system_prompt=None,
    )


@timed_operation("ensure_chat_generation")
async def ensure_chat_generation(
    user_id: int,
    *,
    expected_epoch: int | None,
) -> int | None:
    """Return an exact live chat generation, creating only a known-missing row.

    ``expected_epoch=None`` is an explicit first-chat signal from
    :func:`get_user_chat`; it is never inferred from the numeric value zero,
    which remains a valid legacy generation.  The advisory lock linearizes the
    initial INSERT with account erasure and lease acquisition.
    """
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            if expected_epoch is None:
                await db_query(
                    """
                    INSERT INTO public.chats (user_id)
                    SELECT account.user_id
                    FROM public.users AS account
                    WHERE account.user_id = $1
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (user_id,),
                    conn=conn,
                )
            rows = await db_query(
                """
                SELECT chat.memory_epoch
                FROM public.chats AS chat
                JOIN public.users AS account ON account.user_id = chat.user_id
                WHERE chat.user_id = $1
                  AND chat.private_data_blocked IS FALSE
                  AND ($2::bigint IS NULL OR chat.memory_epoch = $2)
                """,
                (user_id, expected_epoch),
                conn=conn,
            )
            return int(rows[0]["memory_epoch"]) if rows else None
        finally:
            await clear_user_context(conn=conn)


@timed_operation("set_ltm_enabled")
async def set_ltm_enabled(user_id: int, enabled: bool) -> int:
    """Atomically update consent without a stale full-state chat overwrite.

    Migration 067 owns ``memory_epoch`` changes through its trigger.  Returning
    the trigger-managed value lets handlers keep their local ChatState coherent.
    """
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            rows = await db_query(
                """
                INSERT INTO public.chats (user_id, ltm_enabled)
                VALUES ($1, $2)
                ON CONFLICT (user_id)
                DO UPDATE SET ltm_enabled = EXCLUDED.ltm_enabled
                WHERE public.chats.private_data_blocked IS FALSE
                RETURNING memory_epoch
                """,
                (user_id, enabled),
                conn=conn,
            )
            if not rows:
                raise RuntimeError("LTM consent update returned no row")
            memory_epoch = int(rows[0]["memory_epoch"] or 0)
        finally:
            await clear_user_context(conn=conn)

    if not enabled:
        from app.repos.memory_consent import wait_for_private_data_leases

        await wait_for_private_data_leases(
            user_id,
            before_epoch=memory_epoch,
            ltm_only=True,
        )
        from app.voice_engine import get_voice_reply_manager

        await get_voice_reply_manager().purge_user_jobs(user_id, ltm_only=True)
    return memory_epoch


@timed_operation("replace_context_summary")
async def replace_context_summary(
    user_id: int,
    *,
    expected_summary: str | None,
    new_summary: str,
    expected_epoch: int,
) -> bool:
    """CAS-update only the summary, rejecting a stale background result."""
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            rows = await db_query(
                """
                UPDATE public.chats
                SET context_summary = $3
                WHERE user_id = $1
                  AND context_summary IS NOT DISTINCT FROM $2
                  AND memory_epoch = $4
                  AND private_data_blocked IS FALSE
                RETURNING user_id
                """,
                (user_id, expected_summary, new_summary, expected_epoch),
                conn=conn,
            )
            return bool(rows)
        finally:
            await clear_user_context(conn=conn)


@timed_operation("update_user_chat")
async def update_user_chat(
    user_id: int,
    chat_state: ChatState,
    *,
    rewrite_history: bool = False,
    expected_epoch: int | None = None,
) -> bool:
    """Save chat state, optionally replacing the canonical message sequence.

    ``rewrite_history`` is required when context compaction removes a prefix.
    Length comparison alone cannot distinguish that rewrite from ordinary
    append-only growth when one or two new turns are added in the same request.
    """
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            effective_expected_epoch = expected_epoch
            if effective_expected_epoch is None and getattr(chat_state, "_has_persisted_chat", False) is True:
                effective_expected_epoch = int(chat_state.memory_epoch)
            current_length = len(chat_state.history)
            original_length = getattr(chat_state, "_original_length", 0)

            should_delete = False
            # ⚡ Bolt Optimization: Use parallel arrays instead of JSON for ~13x faster
            # Python serialization and much cheaper Postgres `unnest` vs `json_to_recordset`
            roles_to_insert = []
            contents_to_insert = []

            if rewrite_history:
                should_delete = True
                for msg in chat_state.history:
                    roles_to_insert.append(msg.get("role", "user"))
                    contents_to_insert.append(_extract_message_content(msg))
            elif current_length == 0 and original_length > 0:
                should_delete = True
            elif current_length > original_length:
                new_msgs = chat_state.history[original_length:]
                for msg in new_msgs:
                    roles_to_insert.append(msg.get("role", "user"))
                    contents_to_insert.append(_extract_message_content(msg))
            elif current_length < original_length:
                should_delete = True
                for msg in chat_state.history:
                    roles_to_insert.append(msg.get("role", "user"))
                    contents_to_insert.append(_extract_message_content(msg))

            chat_state._original_length = current_length

            query = """
            WITH update_chats AS (
                INSERT INTO public.chats (
                    user_id, model, token_count, search_enabled, system_prompt, context_summary,
                    thinking_level, ltm_enabled, branch_id, temperature, voice_id, tts_temperature,
                    live_voice_name, live_thinking_level, live_connection_mode
                )
                SELECT $1, $2, $3, $4, $5, $6, $7, $8, $14, $15, $16, $17, $18, $19, $20
                WHERE $21::bigint IS NULL
                   OR EXISTS (
                       SELECT 1 FROM public.chats AS current_chat
                       WHERE current_chat.user_id = $1
                         AND current_chat.memory_epoch = $21
                         AND current_chat.private_data_blocked IS FALSE
                   )
                ON CONFLICT (user_id)
                DO UPDATE SET
                    model = EXCLUDED.model, token_count = EXCLUDED.token_count,
                    search_enabled = EXCLUDED.search_enabled, system_prompt = EXCLUDED.system_prompt,
                    context_summary = EXCLUDED.context_summary, thinking_level = EXCLUDED.thinking_level,
                    branch_id = EXCLUDED.branch_id,
                    temperature = EXCLUDED.temperature, voice_id = EXCLUDED.voice_id,
                    tts_temperature = EXCLUDED.tts_temperature, live_voice_name = EXCLUDED.live_voice_name,
                    live_thinking_level = EXCLUDED.live_thinking_level,
                    live_connection_mode = EXCLUDED.live_connection_mode
                WHERE $21::bigint IS NULL
                   OR (
                       public.chats.memory_epoch = $21
                       AND public.chats.private_data_blocked IS FALSE
                   )
                RETURNING memory_epoch
            ),
            update_users AS (
                UPDATE public.users SET is_deep_dive = $9, deep_dive_thread_id = $10
                WHERE user_id = $1 AND EXISTS (SELECT 1 FROM update_chats)
            ),
            delete_messages AS (
                DELETE FROM public.active_chat_messages
                WHERE user_id = $1 AND $11 AND EXISTS (SELECT 1 FROM update_chats)
            ),
            insert_messages AS (
                INSERT INTO public.active_chat_messages (user_id, role, content)
                SELECT $1, role, content FROM unnest($12::text[], $13::text[]) AS x(role, content)
                WHERE array_length($12::text[], 1) > 0
                  AND EXISTS (SELECT 1 FROM update_chats)
                RETURNING user_id
            )
            SELECT memory_epoch FROM update_chats;
            """

            rows = await db_query(
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
                    roles_to_insert,
                    contents_to_insert,
                    chat_state.branch_id,
                    chat_state.temperature,
                    chat_state.voice_id,
                    chat_state.tts_temperature,
                    chat_state.live_voice_name,
                    chat_state.live_thinking_level,
                    chat_state.live_connection_mode,
                    effective_expected_epoch,
                ),
                conn=conn,
            )
            if rows:
                chat_state.memory_epoch = int(rows[0]["memory_epoch"])
                chat_state._has_persisted_chat = True
                return True
            return False
        finally:
            await clear_user_context(conn=conn)


async def migrate_invalid_models(
    available_models: set[str] | list[str],
    default_gemini_model: str,
    default_openrouter_model: str,
    default_opencode_model: str = "",
    default_freetheai_model: str = "",
) -> int:
    """Migrate users whose active model is no longer in the available set.

    Returns the number of migrated users.
    """
    if not available_models or not db_manager.is_connected:
        return 0

    available_order = sorted(available_models) if isinstance(available_models, set) else list(available_models)
    available_order = list(dict.fromkeys(available_order))
    available_set = set(available_order)

    def migration_target(default_model: str, predicate) -> str:
        if default_model in available_set:
            return default_model
        return next((model for model in available_order if predicate(model)), available_order[0])

    gemini_target = migration_target(
        default_gemini_model,
        lambda model: not is_opencode_model(model) and not is_freetheai_model(model) and not is_openrouter_model(model),
    )
    openrouter_target = migration_target(default_openrouter_model, is_openrouter_model)
    opencode_target = migration_target(default_opencode_model, is_opencode_model)
    freetheai_target = migration_target(default_freetheai_model, is_freetheai_model)

    placeholders = ",".join([f"${i + 1}" for i in range(len(available_order))])
    invalid_chats = await db_query(
        f"""
        SELECT user_id, model
        FROM public.chats
        WHERE model IS NOT NULL
        AND model NOT IN ({placeholders})
        """,
        tuple(available_order),
    )

    migrated = 0
    gemini_users = []
    openrouter_users = []
    opencode_users = []
    freetheai_users = []
    for chat in invalid_chats:
        user_id = chat["user_id"]
        old_model = chat["model"]
        if is_opencode_model(old_model):
            opencode_users.append(user_id)
        elif is_freetheai_model(old_model):
            freetheai_users.append(user_id)
        elif is_openrouter_model(old_model):
            openrouter_users.append(user_id)
        else:
            gemini_users.append(user_id)
        logging.info("Migrating user %s from %s", user_id, old_model)

    if gemini_users:
        await db_query(
            "UPDATE public.chats SET model = $1 WHERE user_id = ANY($2)",
            (gemini_target, gemini_users),
        )
        migrated += len(gemini_users)

    if openrouter_users:
        await db_query(
            "UPDATE public.chats SET model = $1 WHERE user_id = ANY($2)",
            (openrouter_target, openrouter_users),
        )
        migrated += len(openrouter_users)

    if opencode_users:
        await db_query(
            "UPDATE public.chats SET model = $1 WHERE user_id = ANY($2)",
            (opencode_target, opencode_users),
        )
        migrated += len(opencode_users)

    if freetheai_users:
        await db_query(
            "UPDATE public.chats SET model = $1 WHERE user_id = ANY($2)",
            (freetheai_target, freetheai_users),
        )
        migrated += len(freetheai_users)

    if migrated:
        logging.warning("Migrated %d users to default models after config reload", migrated)
    return migrated


async def model_migration_watcher(old_settings, new_settings) -> None:
    """Config watcher: migrates users whose model is no longer available.

    Registered via ``config_manager.add_watcher()`` at startup so that config.py
    never imports from the DB/repos layer directly (AR-4).
    """
    try:
        from app.repos.models_repo import sync_models_from_db

        await sync_models_from_db(new_settings)
        all_available = list(
            dict.fromkeys(
                [
                    *new_settings.AVAILABLE_MODELS,
                    *new_settings.OPENROUTER_AVAILABLE_MODELS,
                    *new_settings.OPENCODE_AVAILABLE_MODELS,
                    *new_settings.FREETHEAI_AVAILABLE_MODELS,
                ]
            )
        )

        await migrate_invalid_models(
            available_models=all_available,
            default_gemini_model=new_settings.DEFAULT_MODEL,
            default_openrouter_model=new_settings.OPENROUTER_DEFAULT_MODEL,
            default_opencode_model=new_settings.OPENCODE_DEFAULT_MODEL,
            default_freetheai_model=new_settings.FREETHEAI_DEFAULT_MODEL,
        )
    except Exception as e:
        logging.error("Model migration watcher error: %s", e)


async def update_thinking_level(user_id: int, level: str | None) -> None:
    """Update thinking level for a user's chat. None resets to default."""
    await db_query(
        "UPDATE public.chats SET thinking_level = $1 WHERE user_id = $2",
        (level, user_id),
    )
