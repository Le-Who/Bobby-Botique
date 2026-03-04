"""
Conversation CRUD: save, load, switch, rename, delete conversations.

Also includes role data lookup (system roles + user custom roles).

Extracted from app/database.py to isolate conversation-domain logic.
"""

import logging
from typing import Any

import asyncpg

from app.database import (
    db_execute_many,
    db_manager,
    db_query,
    reconnect_database,
)
from app.utils.logging_config import timed_operation


async def get_role_data(role_key: str, user_id: int) -> dict[str, Any] | None:
    """
    Получает data roles (название, промпт) по keyу.
    Поддерживает системные roles (from prompts.py) и userские (from БД).
    """
    from app import prompts

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
    elif role_key in prompts.DEFAULT_ROLES:
        meta = prompts.DEFAULT_ROLES[role_key]
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
            """INSERT INTO conversations (user_id, title, role_type, role_id, summary, token_budget, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP) RETURNING id""",
            (user_id, title, role_type, role_id, None, chat_state.token_count),
        )
        conv_id = result[0]["id"] if result else None
        if conv_id and chat_state.history:
            try:
                await db_query("CALL save_chat_to_conversation($1, $2)", (user_id, conv_id))
            except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                logging.error("Error saving conversation messages via Procedure: %s", e, exc_info=True)
        return conv_id
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.error("Error in save_conversation: %s", e, exc_info=True)
        return None


async def get_user_conversations(user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
    try:
        result = await db_query(
            """SELECT c.id, c.title, c.role_type, c.role_id, c.summary, c.token_budget, c.created_at,
                      r.title as role_title, ur.title as user_role_title
               FROM conversations c
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
            FROM conversations c
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
            conv_data = await db_query(
                "SELECT role_type, role_id, summary FROM conversations WHERE id = $1 AND user_id = $2",
                (conversation_id, user_id),
                conn=conn,
            )
            if not conv_data:
                return False
            role_type, role_id, _ = (
                conv_data[0]["role_type"],
                conv_data[0]["role_id"],
                conv_data[0]["summary"],
            )
            messages = await get_conversation_messages(conversation_id, user_id, conn=conn)
            if messages is None:
                return False

            await db_query(
                "DELETE FROM active_chat_messages WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            if messages:
                insert_data = [(user_id, msg["role"], str(msg.get("content", ""))) for msg in messages]
                await db_execute_many(
                    "INSERT INTO active_chat_messages (user_id, role, content) VALUES ($1, $2, $3)",
                    insert_data,
                    conn=conn,
                )

            await db_query(
                "UPDATE chats SET token_count = 0 WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )

            if role_type and role_id:
                role_data = None
                if role_type == "role":
                    role_data = await db_query(
                        "SELECT prompt FROM roles WHERE id = $1",
                        (role_id,),
                        conn=conn,
                    )
                elif role_type == "user_role":
                    role_data = await db_query(
                        "SELECT prompt FROM user_roles WHERE id = $1",
                        (role_id,),
                        conn=conn,
                    )

                if role_data:
                    await db_query(
                        "UPDATE chats SET system_prompt = $1 WHERE user_id = $2",
                        (role_data[0]["prompt"], user_id),
                        conn=conn,
                    )

        # Invalidate cache outside the transaction (non-critical)
        async with db_manager._cache_lock:
            if hasattr(db_manager, "_active_chats_cache") and user_id in db_manager._active_chats_cache:
                del db_manager._active_chats_cache[user_id]

        return True
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def rename_conversation(user_id: int, conversation_id: int, new_title: str) -> bool:
    try:
        result = await db_query(
            "UPDATE conversations SET title = $1 WHERE id = $2 AND user_id = $3",
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
                "DELETE FROM conversation_messages WHERE conversation_id = $1 "
                "AND conversation_id IN (SELECT id FROM conversations WHERE id = $1 AND user_id = $2)",
                (conversation_id, user_id),
                conn=conn,
            )
            result = await db_query(
                "DELETE FROM conversations WHERE id = $1 AND user_id = $2 RETURNING id",
                (conversation_id, user_id),
                conn=conn,
            )
            return bool(result)
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return False


async def get_conversation_count(user_id: int) -> int:
    try:
        result = await db_query("SELECT COUNT(*) FROM conversations WHERE user_id = $1", (user_id,))
        return result[0]["count"] if result else 0
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        return 0
