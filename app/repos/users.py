"""
User authentication, authorization, state persistence, and feedback.

Extracted from app/database.py to isolate user-domain business logic
from database infrastructure.
"""

import json
import logging
from typing import Any

import asyncpg

from app.config import settings
from app.database import (
    clear_user_context,
    db_manager,
    db_query,
    reconnect_database,
    set_user_context,
)


def is_admin(user_id: int) -> bool:
    return user_id == settings.ADMIN_ID


async def is_authorized(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    # Check cache
    async with db_manager._cache_lock:
        if user_id in db_manager._user_auth_cache:
            return db_manager._user_auth_cache[user_id]

    if not db_manager.is_connected:
        await reconnect_database()

    async with db_manager.pool.acquire() as conn:
        await set_user_context(user_id, False, conn=conn)
        try:
            result = await db_query(
                "SELECT is_authorized FROM users WHERE user_id = $1",
                (user_id,),
                conn=conn,
            )
            is_auth = result and result[0]["is_authorized"] == 1

            # Update cache
            async with db_manager._cache_lock:
                db_manager._user_auth_cache[user_id] = is_auth

            return is_auth
        finally:
            await clear_user_context(conn=conn)


async def invalidate_user_auth_cache(user_id: int) -> None:
    async with db_manager._cache_lock:
        if user_id in db_manager._user_auth_cache:
            del db_manager._user_auth_cache[user_id]


async def load_user_state(user_id: int) -> dict[str, Any] | None:
    """Load persisted user state from the database.

    Returns a dict of state fields or None if no saved state exists.
    """
    try:
        result = await db_query(
            """
            SELECT document_mode, selected_document_id,
                   awaiting_custom_role_input, generated_role,
                   last_custom_role_prompt, generating_custom_role,
                   last_sent_message_text,
                   awaiting_manual_role_title, awaiting_manual_role_prompt,
                   manual_role_title, manual_role_prompt
            FROM user_state WHERE user_id = $1
            """,
            (user_id,),
        )
        if result:
            row = result[0]
            try:
                from app.core.entities import UserStateRow

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
                    "generated_role": row.get("generated_role"),  # JSONB → dict
                    "last_custom_role_prompt": row.get("last_custom_role_prompt"),
                    "generating_custom_role": row.get("generating_custom_role", False) or False,
                    "last_sent_message_text": row.get("last_sent_message_text"),
                    "awaiting_manual_role_title": row.get("awaiting_manual_role_title", False) or False,
                    "awaiting_manual_role_prompt": row.get("awaiting_manual_role_prompt", False) or False,
                    "manual_role_title": row.get("manual_role_title", "") or "",
                    "manual_role_prompt": row.get("manual_role_prompt", "") or "",
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
) -> None:
    """Persist user state to the database using UPSERT."""
    try:
        role_json = json.dumps(generated_role) if generated_role else None

        await db_query(
            """
            INSERT INTO user_state (
                user_id, document_mode, selected_document_id,
                awaiting_custom_role_input, generated_role,
                last_custom_role_prompt, generating_custom_role,
                last_sent_message_text,
                awaiting_manual_role_title, awaiting_manual_role_prompt,
                manual_role_title, manual_role_prompt,
                updated_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, CURRENT_TIMESTAMP)
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
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                document_mode,
                selected_document_id,
                awaiting_custom_role_input,
                role_json,
                last_custom_role_prompt,
                generating_custom_role,
                last_sent_message_text,
                awaiting_manual_role_title,
                awaiting_manual_role_prompt,
                manual_role_title,
                manual_role_prompt,
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
