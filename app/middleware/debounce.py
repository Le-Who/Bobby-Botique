# app/middleware/debounce.py
"""Message debounce — merges rapid-fire text messages from the same user.

When a user sends multiple short messages in quick succession (split-tapping
or sending sentence fragments), the bot would normally process each as a
separate AI request, wasting tokens and producing fragmented responses.

This module provides a 1.1s trailing aggregation window: if a second text message
arrives from the same user within 1.1s of the latest, both texts are
concatenated and the window is reset (trailing debounce). A single request is processed
when no new messages arrive for 1.1s.

Usage from messages.py::

    from app.middleware.debounce import debounce_text_message

    merged = await debounce_text_message(user_id, message_text)
    if merged is None:
        return  # message was buffered; a subsequent call will fire
    # merged contains the concatenated text
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Debounce window in seconds (1.1s balances latency vs merge benefit, covers Telegram forward bursts)
_DEBOUNCE_WINDOW_S = 1.1

# Per-user debounce state: {user_id: _DebounceSlot}
_debounce_slots: dict[int, _DebounceSlot] = {}


class _DebounceSlot:
    """Per-user debounce accumulator."""

    __slots__ = ("texts", "first_ts", "timer_task", "ready_event")

    def __init__(self, text: str) -> None:
        self.texts: list[str] = [text]
        self.first_ts: float = time.monotonic()
        self.timer_task: asyncio.Task[None] | None = None
        self.ready_event: asyncio.Event = asyncio.Event()


async def debounce_text_message(user_id: int, text: str) -> str | None:
    """Buffer a text message and wait for the debounce window to close.

    Returns:
        - The merged text when the window fires (may be a single message).
        - ``None`` if this message was absorbed into an existing window
          (the first caller will eventually receive the merged result).

    Caller contract:
        If ``None`` is returned, the caller should ``return`` immediately
        (the first accumulated call will handle the merged text).
    """
    slot = _debounce_slots.get(user_id)

    # Helper to restart the timer
    async def _timer() -> None:
        await asyncio.sleep(_DEBOUNCE_WINDOW_S)
        if slot is not None:
            slot.ready_event.set()

    if slot is not None and not slot.ready_event.is_set():
        # Window is still open → absorb this message
        slot.texts.append(text)
        logger.debug(
            "Debounce: absorbed message for user %d (now %d parts)",
            user_id,
            len(slot.texts),
        )

        # Trailing window: restart the timer
        if slot.timer_task is not None:
            slot.timer_task.cancel()

        slot.timer_task = asyncio.create_task(_timer())
        return None

    # First message in a new window → create slot
    slot = _DebounceSlot(text)
    _debounce_slots[user_id] = slot

    # Start the window timer
    slot.timer_task = asyncio.create_task(_timer())

    # Wait for window to close
    await slot.ready_event.wait()

    # Harvest and clean up
    merged = "\n".join(slot.texts)
    _debounce_slots.pop(user_id, None)

    if len(slot.texts) > 1:
        logger.info(
            "Debounce: merged %d messages for user %d (%d chars total)",
            len(slot.texts),
            user_id,
            len(merged),
        )

    return merged
