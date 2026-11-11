"""User reminders — DB-persisted scheduled follow-ups.

Handles CRUD for user reminders (Feature 5: Proactive Follow-ups).
Reminders are polled every 60s by the queue system and delivered
via the bot when ``trigger_at`` has passed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.config import UTC_TZ
from app.database import db_query
from app.utils.json_compat import json

logger = logging.getLogger(__name__)


async def create_reminder(
    user_id: int,
    trigger_at: datetime,
    prompt: str,
    context_history: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> int | None:
    """Create a new reminder.

    Returns the reminder_id or None on failure.
    """
    try:
        ctx_json = json.dumps(context_history, ensure_ascii=False) if context_history else None
        result = await db_query(
            """
            INSERT INTO user_reminders (user_id, trigger_at, prompt, context_history, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            RETURNING id
            """,
            (user_id, trigger_at, prompt, ctx_json),
        )
        if result:
            rid = result[0]["id"]
            logger.info(
                "Reminder created: user=%s id=%d trigger_at=%s prompt=%s",
                user_id,
                rid,
                trigger_at.isoformat(),
                prompt[:60],
            )
            return rid
        return None
    except Exception as e:
        logger.error("Failed to create reminder for user %s: %s", user_id, e)
        return None


async def get_pending_reminders() -> list[dict[str, Any]]:
    """Get all reminders whose trigger_at has passed and are not yet delivered."""
    now = datetime.now(UTC_TZ)
    rows = await db_query(
        """SELECT id, user_id, prompt, context_history, trigger_at
           FROM user_reminders
           WHERE trigger_at <= $1 AND is_delivered = FALSE
             AND NOT EXISTS (SELECT 1 FROM public.chats c
                             WHERE c.user_id=user_reminders.user_id AND c.private_data_blocked)
             AND NOT EXISTS (SELECT 1 FROM public.scheduled_deliveries d
                             WHERE d.kind='reminder' AND d.source_id=user_reminders.id
                               AND NOT (d.state='failed' AND d.retryable AND d.retry_at<=$1))
           ORDER BY trigger_at ASC
           LIMIT 50""",
        (now,),
    )
    return [dict(r) for r in rows]


async def get_user_reminders(user_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
    """Include terminal delivery problems so they do not silently disappear."""
    rows = await db_query(
        """SELECT r.id, r.prompt, r.trigger_at, r.is_delivered, r.context_history,
                  d.state AS delivery_state, d.retryable
           FROM user_reminders r LEFT JOIN public.scheduled_deliveries d
             ON d.kind='reminder' AND d.source_id=r.id
           WHERE r.user_id = $1 AND r.is_delivered = FALSE
           ORDER BY r.trigger_at ASC LIMIT $2""",
        (user_id, limit),
    )
    return [dict(r) for r in rows]


async def delete_reminder(reminder_id: int, user_id: int) -> bool:
    """Delete a reminder."""
    try:
        await db_query(
            "DELETE FROM user_reminders WHERE id = $1 AND user_id = $2",
            (reminder_id, user_id),
        )
        return True
    except Exception:
        return False
