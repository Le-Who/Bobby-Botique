from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import state
from app.handlers import cb_ai_actions, messages
from app.utils import heartbeat


@pytest.mark.asyncio
async def test_long_wait_notice_offers_cancel_then_retry_only_while_pending() -> None:
    placeholder = SimpleNamespace(message_id=42, edit_text=AsyncMock())
    pending = asyncio.Event()

    await messages.show_long_wait_retry(placeholder, pending, delay_seconds=0.01)

    markup = placeholder.edit_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "retry_wait:42"

    pending.set()
    placeholder.edit_text.reset_mock()
    await messages.show_long_wait_retry(placeholder, pending, delay_seconds=0.01)
    placeholder.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_long_wait_stops_matching_request_before_offering_retry() -> None:
    user_id = 900007
    query = SimpleNamespace(
        data="retry_wait:42",
        message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=user_id)),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user_id))
    lock = state.get_user_lock(user_id)
    entered = asyncio.Event()

    async def active_request() -> None:
        async with lock:
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(active_request())
    await entered.wait()
    state.register_active_task(user_id, task)
    state.set_last_bot_message(user_id, 42, user_id)
    heartbeat.register_heartbeat(42, asyncio.Event())
    try:
        await cb_ai_actions.cancel_long_wait_callback(update, SimpleNamespace())
    finally:
        await asyncio.gather(task, return_exceptions=True)
        state.clear_active_task(user_id)
        state.clear_last_bot_message(user_id)
        heartbeat.unregister_heartbeat(42)
    assert task.cancelled()
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "retry_last" in [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_stale_long_wait_button_cannot_cancel_new_request() -> None:
    query = SimpleNamespace(
        data="retry_wait:42",
        message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=7)),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7))
    with (
        patch("app.handlers.cb_ai_actions.state.get_last_bot_message", return_value=(99, 7)),
        patch("app.utils.heartbeat.is_heartbeat_active", return_value=True),
        patch("app.handlers.cb_ai_actions.state.cancel_active_task") as cancel,
    ):
        await cb_ai_actions.cancel_long_wait_callback(update, SimpleNamespace())
    cancel.assert_not_called()
    query.edit_message_text.assert_not_awaited()
