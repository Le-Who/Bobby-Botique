"""Conversation branching — fork and restore chat state snapshots.

Provides a lightweight branching mechanism: users can fork the current
conversation into a temporary "what-if" branch, then return to the main
thread. Under the hood, this snapshots ``chat_state.history`` into the
``conversation_branches`` table and restores on demand.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.database import db_query

logger = logging.getLogger(__name__)


async def create_branch(user_id: int, history: list[dict[str, Any]], *, label: str = "auto") -> int | None:
    """Snapshot current history into a branch row.

    Returns the branch_id or None on failure.
    """
    try:
        result = await db_query(
            """
            INSERT INTO conversation_branches (user_id, label, snapshot_history, created_at)
            VALUES ($1, $2, $3, NOW())
            RETURNING id
            """,
            (user_id, label, json.dumps(history, ensure_ascii=False)),
        )
        if result:
            branch_id = result[0]["id"]
            logger.info("Branch created: user=%s branch_id=%d label=%s msgs=%d", user_id, branch_id, label, len(history))
            return branch_id
        return None
    except Exception as e:
        logger.error("Failed to create branch for user %s: %s", user_id, e)
        return None


async def restore_branch(user_id: int, branch_id: int) -> list[dict[str, Any]] | None:
    """Restore history from a branch snapshot.

    Returns the snapshot history list or None if not found.
    """
    try:
        result = await db_query(
            "SELECT snapshot_history FROM conversation_branches WHERE id = $1 AND user_id = $2",
            (branch_id, user_id),
        )
        if result:
            history = json.loads(result[0]["snapshot_history"])
            logger.info("Branch restored: user=%s branch_id=%d msgs=%d", user_id, branch_id, len(history))
            return history
        return None
    except Exception as e:
        logger.error("Failed to restore branch %d for user %s: %s", branch_id, user_id, e)
        return None


async def get_active_branch(user_id: int) -> dict[str, Any] | None:
    """Get the most recent branch for a user (if any)."""
    result = await db_query(
        """SELECT id, label, created_at
           FROM conversation_branches
           WHERE user_id = $1
           ORDER BY created_at DESC
           LIMIT 1""",
        (user_id,),
    )
    return dict(result[0]) if result else None


async def delete_branch(branch_id: int, user_id: int) -> bool:
    """Delete a branch after it's been restored."""
    try:
        await db_query(
            "DELETE FROM conversation_branches WHERE id = $1 AND user_id = $2",
            (branch_id, user_id),
        )
        return True
    except Exception:
        return False
