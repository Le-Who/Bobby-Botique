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

# Request identity also distinguishes equal message IDs in different chats.
_HEARTBEAT_OWNERS: dict[tuple[int, int], asyncio.Event] = {}
_WAIT_NOTICE_TASKS: dict[asyncio.Event, asyncio.Task] = {}


def register_heartbeat(message_id: int, event: asyncio.Event, chat: Any = None) -> None:
    """Registers a heartbeat cancellation event for a given placeholder message_id."""
    _HEARTBEAT_EVENTS[message_id] = event
    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        _HEARTBEAT_OWNERS[chat_id, message_id] = event

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


def unregister_heartbeat(message_id: int, *, event: asyncio.Event | None = None) -> None:
    """Removes a heartbeat event without triggering it (e.g. for cleanup)."""
    current = _HEARTBEAT_EVENTS.get(message_id)
    if event is None or current is event:
        _HEARTBEAT_EVENTS.pop(message_id, None)
        task = _HEARTBEAT_TASKS.pop(message_id, None)
        if task and not task.done():
            task.cancel()
    for key, owner in list(_HEARTBEAT_OWNERS.items()):
        if key[1] == message_id and (event is None or owner is event):
            _HEARTBEAT_OWNERS.pop(key)
    if event is not None:
        _WAIT_NOTICE_TASKS.pop(event, None)


def is_heartbeat_active(message_id: int, *, chat_id: int | None = None) -> bool:
    """Whether a placeholder is still waiting for its first answer text."""
    event = _heartbeat_event(message_id, chat_id=chat_id)
    return event is not None and not event.is_set()


def stop_heartbeat(message_id: int, *, event: asyncio.Event | None = None) -> None:
    """
    Stops the heartbeat associated with the given message_id.
    Should be called immediately before modifying the placeholder message
    with terminal content (like the AI response or an error).
    """
    current = _HEARTBEAT_EVENTS.get(message_id)
    if event is None:
        event = current
    if current is event:
        _HEARTBEAT_EVENTS.pop(message_id, None)
        task = _HEARTBEAT_TASKS.pop(message_id, None)
        if task and not task.done():
            task.cancel()
    if event and not event.is_set():
        event.set()
        logging.debug("Heartbeat manually stopped for message %s", message_id)


def _heartbeat_event(message_id: int, *, chat_id: int | None = None) -> asyncio.Event | None:
    if chat_id is not None:
        event = _HEARTBEAT_OWNERS.get((chat_id, message_id))
        if event is not None:
            return event
        if any(key[1] == message_id for key in _HEARTBEAT_OWNERS):
            return None
    return _HEARTBEAT_EVENTS.get(message_id)


def register_wait_notice(event: asyncio.Event, task: asyncio.Task, coroutine: Any) -> None:
    """Retain the manager task and close its raw input even before first step."""
    _WAIT_NOTICE_TASKS[event] = task
    task.add_done_callback(lambda _task: coroutine.close())


async def retire_heartbeat(message_id: int, *, chat_id: int | None = None, event: asyncio.Event | None = None) -> None:
    """Join cooperative notice cleanup before the placeholder's next writer."""
    event = event if event is not None else _heartbeat_event(message_id, chat_id=chat_id)
    if event is None:
        return
    stop_heartbeat(message_id, event=event)
    task = _WAIT_NOTICE_TASKS.get(event)
    if task is None:
        return
    if not task.done() and not task.cancelling():
        task.cancel()
    # Include the raw-coroutine closure callback even for an already-done task.
    completion: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    task.add_done_callback(lambda _task: completion.set_result(None))
    cancellation = None
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    completion.result()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        task.result()
    if cancellation is not None:
        raise cancellation
