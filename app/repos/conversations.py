"""
Conversation CRUD: save, load, switch, rename, delete conversations.

Also includes role data lookup (system roles + user custom roles).

Extracted from app/database.py to isolate conversation-domain logic.
"""

import logging
from typing import Any

import asyncpg

from app.database import (
    clear_user_context,
    db_execute_many,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)
from app.utils.json_compat import json
from app.utils.logging_config import timed_operation


async def get_role_data(role_key: str, user_id: int) -> dict[str, Any] | None:
    """
    Получает data roles (название, промпт) по keyу.
    Поддерживает системные roles (from prompts.py) и userские (from БД).
    """
    from app.prompt_registry import DEFAULT_ROLES

    if not role_key:
        return None

    if role_key.startswith("user_role:"):
        try:
            role_id = int(role_key.split(":")[1])
            res = await db_query(
                "SELECT id, title, prompt FROM user_roles WHERE id = $1 AND user_id = $2",
                (role_id, user_id),
            )
            if res:
                return {
                    "id": res[0]["id"],
                    "title": res[0]["title"],
                    "prompt": res[0]["prompt"],
                    "is_custom": True,
                    "key": role_key,
                }
        except (ValueError, IndexError, asyncpg.PostgresError):
            pass
    elif role_key in DEFAULT_ROLES:
        meta = DEFAULT_ROLES[role_key]
        return {
            "id": None,
            "title": meta.get("title", role_key),
            "prompt": meta.get("prompt", ""),
            "is_custom": False,
            "key": role_key,
        }

    return None


@timed_operation("save_conversation")
async def save_conversation(
    user_id: int, title: str, role_type: str | None = None, role_id: int | None = None
) -> int | None:
    try:
        from app.repos.chats import get_user_chat

        chat_state = await get_user_chat(user_id)
        if not chat_state:
            return None
        result = await db_query(
            """INSERT INTO public.conversations (user_id, title, role_type, role_id, summary, token_budget, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP) RETURNING id""",
            (user_id, title, role_type, role_id, None, chat_state.token_count),
        )
        conv_id = result[0]["id"] if result else None
        if conv_id and chat_state.history:
            try:
                await db_query("CALL save_chat_to_conversation($1, $2)", (user_id, conv_id))
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error(
                    "Error saving conversation messages via Procedure: %s",
                    e,
                    exc_info=True,
                )
        return conv_id
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Error in save_conversation: %s", e, exc_info=True)
        return None


async def get_user_conversations(user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    try:
        result = await db_query(
            """SELECT c.id, c.title, c.role_type, c.role_id, c.summary, c.token_budget, c.created_at,
                      r.title as role_title, ur.title as user_role_title
               FROM public.conversations c
               LEFT JOIN roles r ON c.role_type = 'role' AND c.role_id = r.id
               LEFT JOIN user_roles ur ON c.role_type = 'user_role' AND c.role_id = ur.id
               WHERE c.user_id = $1
               ORDER BY c.created_at DESC
               LIMIT $2 OFFSET $3""",
            (user_id, limit, offset),
        )
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "role_type": row["role_type"],
                "role_id": row["role_id"],
                "summary": row["summary"],
                "token_budget": row["token_budget"],
                "created_at": row["created_at"],
                "role_title": row["role_title"] or row["user_role_title"],
            }
            for row in result
        ]
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return []


async def get_conversation_messages(conversation_id: int, user_id: int, *, conn=None) -> list[dict[str, Any]] | None:
    try:
        query = """
            SELECT cm.role, cm.content, cm.created_at
            FROM public.conversations c
            LEFT JOIN conversation_messages cm ON c.id = cm.conversation_id
            WHERE c.id = $1 AND c.user_id = $2
            ORDER BY cm.created_at ASC
        """
        result = await db_query(query, (conversation_id, user_id), conn=conn)

        if not result:
            return None

        if result[0]["role"] is None:
            return []

        return [
            {
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in result
        ]
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return None


@timed_operation("switch_to_conversation")
async def switch_to_conversation(user_id: int, conversation_id: int) -> bool:
    try:
        if not db_manager.is_connected:
            await reconnect_database()

        async with db_manager.pool.acquire() as conn, conn.transaction():
            # ⚡ Bolt Optimization: Use LEFT JOIN to fetch conversation and its optional role prompt in 1 query instead of 3.
            query = """
                SELECT c.role_type, c.role_id, c.summary,
                       COALESCE(r.prompt, ur.prompt) as role_prompt
                FROM public.conversations c
                LEFT JOIN roles r ON c.role_type = 'role' AND c.role_id = r.id
                LEFT JOIN user_roles ur ON c.role_type = 'user_role' AND c.role_id = ur.id
                WHERE c.id = $1 AND c.user_id = $2
            """
            conv_data = await db_query(query, (conversation_id, user_id), conn=conn)

            if not conv_data:
                return False

            role_prompt = conv_data[0]["role_prompt"]

            messages = await get_conversation_messages(conversation_id, user_id, conn=conn)
            if messages is None:
                return False

            await db_query(
                "DELETE FROM public.active_chat_messages WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            if messages:
                insert_data = [(user_id, msg["role"], str(msg.get("content", ""))) for msg in messages]
                await db_execute_many(
                    "INSERT INTO public.active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
                    insert_data,
                    conn=conn,
                )

            # ⚡ Bolt Optimization: Combine token_count and system_prompt updates into 1 query
            if role_prompt is not None:
                await db_query(
                    "UPDATE public.chats SET token_count = 0, system_prompt = $1 WHERE user_id = $2",
                    (role_prompt, user_id),
                    conn=conn,
                )
            else:
                await db_query(
                    "UPDATE public.chats SET token_count = 0 WHERE user_id = $1",
                    (user_id,),
                    conn=conn,
                )

        return True
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def rename_conversation(user_id: int, conversation_id: int, new_title: str) -> bool:
    try:
        result = await db_query(
            "UPDATE public.conversations SET title = $1 WHERE id = $2 AND user_id = $3",
            (new_title, conversation_id, user_id),
        )
        return result is not None
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def delete_conversation(user_id: int, conversation_id: int) -> bool:
    try:
        if not db_manager.is_connected:
            await reconnect_database()

        async with db_manager.pool.acquire() as conn, conn.transaction():
            # Single atomic operation: delete messages then conversation
            # No TOCTOU — if the conversation doesn't exist, no harm done
            await db_query(
                "DELETE FROM conversation_messages WHERE conversation_id IN "
                "(SELECT id FROM public.conversations WHERE id = $1 AND user_id = $2)",
                (conversation_id, user_id),
                conn=conn,
            )
            result = await db_query(
                "DELETE FROM public.conversations WHERE id = $1 AND user_id = $2 RETURNING id",
                (conversation_id, user_id),
                conn=conn,
            )
            return bool(result)
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def get_conversation_count(user_id: int) -> int:
    try:
        result = await db_query("SELECT COUNT(*) FROM public.conversations WHERE user_id = $1", (user_id,))
        return result[0]["count"] if result else 0
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return 0


async def export_user_conversations(user_id: int) -> list[dict[str, Any]]:
    """Return every saved conversation and message for a privacy export.

    Unlike the interactive list helpers, export failures intentionally
    propagate: a successful but incomplete GDPR archive is misleading.
    """
    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            rows = await db_query(
                """
                SELECT
                    conversation.id,
                    conversation.title,
                    conversation.role_type,
                    conversation.role_id,
                    conversation.summary,
                    conversation.token_budget,
                    conversation.created_at,
                    COALESCE(
                        jsonb_agg(
                            jsonb_build_object(
                                'role', message.role,
                                'content', message.content,
                                'created_at', message.created_at
                            )
                            ORDER BY message.created_at ASC, message.id ASC
                        ) FILTER (WHERE message.id IS NOT NULL),
                        '[]'::jsonb
                    ) AS messages
                FROM public.conversations AS conversation
                LEFT JOIN public.conversation_messages AS message
                  ON message.conversation_id = conversation.id
                 AND message.owner_user_id = conversation.user_id
                WHERE conversation.user_id = $1
                GROUP BY
                    conversation.id,
                    conversation.title,
                    conversation.role_type,
                    conversation.role_id,
                    conversation.summary,
                    conversation.token_budget,
                    conversation.created_at
                ORDER BY conversation.created_at DESC, conversation.id DESC
                """,
                (user_id,),
                conn=conn,
            )
        finally:
            await clear_user_context(conn=conn)

    exported: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        messages = item.get("messages", [])
        if isinstance(messages, str):
            messages = json.loads(messages)
        item["messages"] = messages or []
        exported.append(item)
    return exported
