"""
Global registry for long-running request heartbeats.
Provides a centralized way to stop placeholder "Thinking..." updates
when the real response is ready to be sent.
"""

import asyncio
import contextlib
import logging
from typing import Any

# Dictionary mapping Telegram message_id to its heartbeat termination Event
_HEARTBEAT_EVENTS: dict[int, asyncio.Event] = {}

# Dictionary mapping Telegram message_id to its active heartbeat task
_HEARTBEAT_TASKS: dict[int, asyncio.Task] = {}


def register_heartbeat(message_id: int, event: asyncio.Event, chat: Any = None) -> None:
    """Registers a heartbeat cancellation event for a given placeholder message_id."""
    _HEARTBEAT_EVENTS[message_id] = event

    if chat:

        async def _heartbeat_loop() -> None:
            try:
                while not event.is_set():
                    with contextlib.suppress(Exception):
                        await chat.send_action(action="typing")
                    try:
                        await asyncio.wait_for(event.wait(), timeout=4.0)
                        break
                    except TimeoutError:
                        continue
            except asyncio.CancelledError:
                pass

        _HEARTBEAT_TASKS[message_id] = asyncio.create_task(_heartbeat_loop())


def unregister_heartbeat(message_id: int) -> None:
    """Removes a heartbeat event without triggering it (e.g. for cleanup)."""
    _HEARTBEAT_EVENTS.pop(message_id, None)
    task = _HEARTBEAT_TASKS.pop(message_id, None)
    if task and not task.done():
        task.cancel()


def stop_heartbeat(message_id: int) -> None:
    """
    Stops the heartbeat associated with the given message_id.
    Should be called immediately before modifying the placeholder message
    with terminal content (like the AI response or an error).
    """
    event = _HEARTBEAT_EVENTS.pop(message_id, None)
    if event and not event.is_set():
        event.set()
        logging.debug("Heartbeat manually stopped for message %s", message_id)

    task = _HEARTBEAT_TASKS.pop(message_id, None)
    if task and not task.done():
        task.cancel()
