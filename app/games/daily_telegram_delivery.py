"""Permanent Telegram recipient failures shared by daily game delivery."""

from __future__ import annotations

from telegram.error import BadRequest, Forbidden

from app.repos import crocodile_daily as repo


def is_unreachable_chat_error(error: BaseException) -> bool:
    """Distinguish an unreachable private chat from a bad photo or transient error."""
    message = str(error).lower()
    if isinstance(error, BadRequest):
        return "chat not found" in message
    if isinstance(error, Forbidden):
        return any(
            phrase in message for phrase in ("bot was blocked", "user is deactivated", "can't initiate conversation")
        )
    return False


async def retire_unreachable_daily_recipient(user_id: int, error: BaseException, *, discovery: bool) -> bool:
    """Stop repeated sends until the player returns or discovery snooze expires."""
    if not is_unreachable_chat_error(error):
        return False
    if discovery:
        await repo.snooze_discovery(user_id)
    else:
        await repo.unsubscribe(user_id)
        await repo.snooze_discovery(user_id)
    return True
