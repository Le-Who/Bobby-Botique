"""Bounded, best-effort Telegram progress indicators for this bot process."""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from time import monotonic
from weakref import WeakKeyDictionary

from telegram import Bot
from telegram.error import RetryAfter, TelegramError

from app.observability.events import emit

_MIN_INTERVAL_S = 4.0
_SEND_TIMEOUT_S = 3.0
_MAX_CHATS = 1024


@dataclass
class _ActionBudget:
    paused_until: float = 0.0
    next_send: dict[int, float] = field(default_factory=dict)
    in_flight: set[int] = field(default_factory=set)


# The application has one bot per loop; do not retain state across loop shutdown.
_budgets: WeakKeyDictionary[asyncio.AbstractEventLoop, _ActionBudget] = WeakKeyDictionary()


async def send_chat_action(bot: Bot, chat_id: int, action: str = "typing") -> bool:
    """Send fresh progress, or skip it without delaying/retrying user work.

    Admission is reserved before transport yields, shared across action types.
    A flood wait pauses all cosmetic actions because Telegram does not identify
    whether the exceeded limit belongs to one chat or the whole bot.
    """
    budget = _budgets.setdefault(asyncio.get_running_loop(), _ActionBudget())
    now = monotonic()
    if now < budget.paused_until or chat_id in budget.in_flight or now < budget.next_send.get(chat_id, 0.0):
        return False
    if len(budget.next_send) >= _MAX_CHATS:
        budget.next_send = {
            chat: deadline for chat, deadline in budget.next_send.items() if deadline > now or chat in budget.in_flight
        }
        if len(budget.next_send) >= _MAX_CHATS:
            return False
    budget.next_send[chat_id] = now + _MIN_INTERVAL_S
    budget.in_flight.add(chat_id)
    try:
        async with asyncio.timeout(_SEND_TIMEOUT_S):
            return await bot.send_chat_action(chat_id=chat_id, action=action)
    except RetryAfter as exc:
        retry_after = exc.retry_after
        delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
        budget.paused_until = max(budget.paused_until, monotonic() + delay)
        emit(
            "telegram.chat_action_rate_limited",
            level="warning",
            operation="telegram.chat_action",
            chat_id=chat_id,
            action=action,
            delay_seconds=delay,
        )
    except (TelegramError, TimeoutError) as exc:
        emit(
            "telegram.chat_action_skipped",
            level="debug",
            operation="telegram.chat_action",
            chat_id=chat_id,
            action=action,
            reason_code=type(exc).__name__,
        )
    finally:
        budget.in_flight.discard(chat_id)
    return False
