"""
Global registry for long-running request heartbeats.
Provides a centralized way to stop placeholder "Thinking..." updates
when the real response is ready to be sent.
"""

import asyncio
import logging
from typing import Dict

# Dictionary mapping Telegram message_id to its heartbeat termination Event
_HEARTBEAT_EVENTS: Dict[int, asyncio.Event] = {}

def register_heartbeat(message_id: int, event: asyncio.Event) -> None:
    """Registers a heartbeat cancellation event for a given placeholder message_id."""
    _HEARTBEAT_EVENTS[message_id] = event

def unregister_heartbeat(message_id: int) -> None:
    """Removes a heartbeat event without triggering it (e.g. for cleanup)."""
    _HEARTBEAT_EVENTS.pop(message_id, None)

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
