"""
User authentication, authorization, state persistence, and feedback.

Extracted from app/database.py to isolate user-domain business logic
from database infrastructure.
"""

import logging
from typing import Any

import asyncpg

from app.config import settings

# Performance: Import Pydantic models at module level to avoid __import__ lock overhead in hot DB paths
from app.core.entities import UserStateRow
from app.database import (
    clear_user_context,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)
from app.utils.json_compat import json


def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    # Check cache
    if user_id in db_manager._user_auth_cache:
        return db_manager._user_auth_cache[user_id]

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn, conn.transaction():
        await set_user_context(user_id, False, conn=conn)
        try:
            result = await db_query(
                "SELECT is_authorized FROM public.users WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            is_auth = result and result[0]["is_authorized"] == 1

            # Update cache
            db_manager._user_auth_cache[user_id] = is_auth

            return is_auth
        finally:
            await clear_user_context(conn=conn)


async def invalidate_user_auth_cache(user_id: int) -> None:
    if user_id in db_manager._user_auth_cache:
        del db_manager._user_auth_cache[user_id]


async def erase_user_account(user_id: int) -> None:
    """Atomically erase a non-admin user and every database-owned record.

    Most user tables are protected by ``ON DELETE CASCADE``.  The explicit
    statements cover legacy tables that predate those foreign keys and shared
    objects whose ownership must be transferred or anonymised first.  Database
    errors propagate so callers can never acknowledge a partial erasure.
    """
    if is_admin(user_id):
        raise ValueError("configured administrator account cannot be self-erased")

    from app.context.summarizer import cancel_user_summarization_tasks
    from app.repos.memory_autosave import cancel_user_memory_tasks

    # Unlike durable LTM writes, summary tasks can hold raw conversation
    # history that is already inside an external provider call.  Cancellation
    # must therefore finish before any account rows are erased.
    await cancel_user_summarization_tasks(user_id)

    try:
        await cancel_user_memory_tasks(user_id)
    except Exception as exc:
        # Durable deletion remains authoritative even if a local task registry
        # is already shutting down or otherwise unavailable.
        logging.warning("Could not cancel LTM tasks before account erasure for %s: %s", user_id, exc)

    if not db_manager.is_connected:
        await reconnect_database()
    if db_manager.pool is None:
        raise RuntimeError("database pool is unavailable for account erasure")

    from app.repos.memory_consent import private_data_barrier

    # The common guard owns phase 1, lease draining, crash-safe takeover, and
    # exact-generation compensation.  It is armed before COMMIT can expose the
    # blocked state.
    async with private_data_barrier(
        user_id,
        is_admin=True,
        ltm_only=False,
    ) as (privacy_barrier, _previous_ltm_enabled):
        from app.voice_engine import get_voice_reply_manager

        await get_voice_reply_manager().purge_user_jobs(user_id, ltm_only=False)

        # Phase 2: reacquire the same lock, verify nobody replaced the
        # generation while leases drained, then perform the atomic destructive
        # work.
        async with db_manager.pool.acquire() as conn, conn.transaction():
            await set_user_context(user_id, True, conn=conn)
            try:
                await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
                barrier_is_current = await db_query(
                """
                SELECT user_id
                FROM public.chats
                WHERE user_id = $1
                  AND memory_epoch = $2
                  AND private_data_blocked IS TRUE
                FOR UPDATE
                """,
                (user_id, privacy_barrier),
                conn=conn,
            )
                if not barrier_is_current:
                    raise RuntimeError("account erasure privacy barrier is no longer current")

                affected_group_rows = await db_query(
                    "SELECT chat_id FROM public.group_members WHERE user_id = $1",
                    (user_id,),
                    conn=conn,
                )
                affected_group_ids = {int(row["chat_id"]) for row in affected_group_rows}

                for statement in (
                    "DELETE FROM public.natal_reports WHERE user_id = $1",
                    "DELETE FROM public.daily_trivia_prompt_messages WHERE user_id = $1",
                    "DELETE FROM public.daily_trivia_super_results WHERE user_id = $1",
                    "DELETE FROM public.inline_boards WHERE creator_id = $1",
                    "UPDATE public.memory_nodes SET actor_user_id = NULL WHERE actor_user_id = $1",
                    "UPDATE public.memory_edges SET actor_user_id = NULL WHERE actor_user_id = $1",
                ):
                    await db_query(statement, (user_id,), conn=conn)

                transferred_rows = await db_query(
                """
                WITH replacements AS (
                    SELECT group_chat.chat_id,
                           (
                               SELECT member.user_id
                               FROM public.group_members AS member
                               JOIN public.users AS candidate
                                 ON candidate.user_id = member.user_id
                               WHERE member.chat_id = group_chat.chat_id
                                 AND member.user_id <> $1
                                 AND candidate.is_authorized = 1
                               ORDER BY member.is_admin DESC,
                                        member.joined_at ASC NULLS LAST,
                                        member.user_id ASC
                               LIMIT 1
                           ) AS replacement_user_id
                    FROM public.group_chats AS group_chat
                    WHERE group_chat.admin_user_id = $1
                ), transferred AS (
                    UPDATE public.group_chats AS group_chat
                    SET admin_user_id = replacement.replacement_user_id
                    FROM replacements AS replacement
                    WHERE group_chat.chat_id = replacement.chat_id
                      AND replacement.replacement_user_id IS NOT NULL
                    RETURNING group_chat.chat_id, group_chat.admin_user_id
                )
                UPDATE public.group_members AS member
                SET is_admin = TRUE
                FROM transferred
                WHERE member.chat_id = transferred.chat_id
                  AND member.user_id = transferred.admin_user_id
                RETURNING transferred.chat_id, transferred.admin_user_id
                """,
                (user_id,),
                conn=conn,
            )
                transferred_admins = {
                    int(row["chat_id"]): int(row["admin_user_id"])
                    for row in transferred_rows
                }

                deleted_group_rows = await db_query(
                "DELETE FROM public.group_chats WHERE admin_user_id = $1 RETURNING chat_id",
                (user_id,),
                conn=conn,
            )
                deleted_group_ids = {int(row["chat_id"]) for row in deleted_group_rows}

                await db_query(
                """
                UPDATE public.group_chats AS group_chat
                SET member_count = GREATEST(group_chat.member_count - 1, 0)
                WHERE EXISTS (
                    SELECT 1
                    FROM public.group_members AS member
                    WHERE member.chat_id = group_chat.chat_id
                      AND member.user_id = $1
                )
                """,
                (user_id,),
                conn=conn,
            )
                await db_query(
                "DELETE FROM public.users WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            finally:
                await clear_user_context(conn=conn)

    # Only clear process-local authorization/state after the transaction has
    # committed.  If the transaction rolls back, the user must remain usable.
    db_manager._user_auth_cache.pop(user_id, None)
    try:
        from app.group_chat import group_chat_manager
        from app.middleware.dedup import clear_user_dedup
        from app.state import purge_user_runtime_state

        purge_user_runtime_state(user_id)
        clear_user_dedup(user_id)
        await group_chat_manager.apply_account_erasure(
            user_id,
            affected_group_ids=affected_group_ids,
            transferred_admins=transferred_admins,
            deleted_group_ids=deleted_group_ids,
        )
    except Exception as exc:
        logging.warning("Account erased, but local cache cleanup failed for %s: %s", user_id, exc)


async def load_user_state(user_id: int) -> dict[str, Any] | None:
    """Load persisted user state from the database.

    Returns a dict of state fields or None if no saved state exists.
    """
    try:
        result = await db_query(
            """
            SELECT *
            FROM user_state WHERE user_id = $1
            """,
            (user_id,),
        )
        if result:
            row = result[0]
            try:
                validated = UserStateRow.model_validate(row)
                return validated.model_dump()
            except Exception as ve:
                logging.warning(
                    "UserStateRow validation failed for user %s, falling back to .get(): %s",
                    user_id,
                    ve,
                )
                return {
                    "document_mode": row.get("document_mode", False) or False,
                    "selected_document_id": row.get("selected_document_id"),
                    "awaiting_custom_role_input": row.get("awaiting_custom_role_input", False) or False,
                    "generated_role": row.get("generated_role"),
                    "last_custom_role_prompt": row.get("last_custom_role_prompt"),
                    "generating_custom_role": row.get("generating_custom_role", False) or False,
                    "last_sent_message_text": row.get("last_sent_message_text"),
                    "awaiting_manual_role_title": row.get("awaiting_manual_role_title", False) or False,
                    "awaiting_manual_role_prompt": row.get("awaiting_manual_role_prompt", False) or False,
                    "manual_role_title": row.get("manual_role_title", "") or "",
                    "manual_role_prompt": row.get("manual_role_prompt", "") or "",
                    "role_diaries": row.get("role_diaries") or {},
                    "tarot_mode": row.get("tarot_mode", False) or False,
                    "tarot_session": row.get("tarot_session"),
                }
        return None
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to load user state for %s: %s", user_id, e)
        return None


async def save_user_state(
    user_id: int,
    document_mode: bool = False,
    selected_document_id: int | None = None,
    awaiting_custom_role_input: bool = False,
    generated_role: dict | None = None,
    last_custom_role_prompt: str | None = None,
    generating_custom_role: bool = False,
    last_sent_message_text: str | None = None,
    awaiting_manual_role_title: bool = False,
    awaiting_manual_role_prompt: bool = False,
    manual_role_title: str = "",
    manual_role_prompt: str = "",
    role_diaries: dict | None = None,
    tarot_mode: bool = False,
    tarot_session: dict | None = None,
) -> None:
    """Persist user state to the database using UPSERT."""
    try:
        await db_query(
            """
            INSERT INTO user_state (
                user_id, document_mode, selected_document_id,
                awaiting_custom_role_input, generated_role,
                last_custom_role_prompt, generating_custom_role,
                last_sent_message_text,
                awaiting_manual_role_title, awaiting_manual_role_prompt,
                manual_role_title, manual_role_prompt,
                role_diaries, tarot_mode, tarot_session,
                updated_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15::jsonb, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE SET
                document_mode = EXCLUDED.document_mode,
                selected_document_id = EXCLUDED.selected_document_id,
                awaiting_custom_role_input = EXCLUDED.awaiting_custom_role_input,
                generated_role = EXCLUDED.generated_role,
                last_custom_role_prompt = EXCLUDED.last_custom_role_prompt,
                generating_custom_role = EXCLUDED.generating_custom_role,
                last_sent_message_text = EXCLUDED.last_sent_message_text,
                awaiting_manual_role_title = EXCLUDED.awaiting_manual_role_title,
                awaiting_manual_role_prompt = EXCLUDED.awaiting_manual_role_prompt,
                manual_role_title = EXCLUDED.manual_role_title,
                manual_role_prompt = EXCLUDED.manual_role_prompt,
                role_diaries = EXCLUDED.role_diaries,
                tarot_mode = EXCLUDED.tarot_mode,
                tarot_session = EXCLUDED.tarot_session,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                document_mode,
                selected_document_id,
                awaiting_custom_role_input,
                generated_role,
                last_custom_role_prompt,
                generating_custom_role,
                last_sent_message_text,
                awaiting_manual_role_title,
                awaiting_manual_role_prompt,
                manual_role_title,
                manual_role_prompt,
                role_diaries or {},
                tarot_mode,
                tarot_session,
            ),
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to save user state for %s: %s", user_id, e)


async def save_feedback(user_id: int, message_id: int, rating: str) -> None:
    """Save user feedback (thumbs up/down) on an AI response."""
    try:
        await db_query(
            "INSERT INTO feedback (user_id, message_id, rating) VALUES ($1, $2, $3)",
            (user_id, message_id, rating),
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        logging.warning("Failed to save feedback for user %s: %s", user_id, e)
