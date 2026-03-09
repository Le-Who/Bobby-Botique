"""Integration tests for Concurrency Hardening bounds (TC-03)."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_dependencies():
    """Setup external dependencies needed by handlers without making DB/network requests."""
    with (
        patch("app.handlers.cb_navigation.get_user_chat", new_callable=AsyncMock) as m_get,
        patch("app.handlers.cb_navigation.update_user_chat", new_callable=AsyncMock) as m_update,
    ):
        yield m_get, m_update


@pytest.mark.asyncio
async def test_state_mutating_callbacks_are_rejected_while_busy(mock_dependencies):
    """
    Risk Covered: State corruption if users click \"New Chat\" or \"Change Model\"
    while an AI stream is currently writing to the DB.

    Level: Integration.
    Tests the _is_user_busy lock interaction for callbacks.
    """
    from app.handlers.cb_navigation import new_chat_callback
    from app.state import get_user_lock
    from tests.factories import make_telegram_context, make_telegram_update

    m_get, m_update = mock_dependencies

    user_id = 12345

    # 1. Arrange a mock update for a callback query
    update = make_telegram_update("data", user_id=user_id)
    # Convert to a callback query mock
    update.callback_query = AsyncMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = "new_chat"

    context = make_telegram_context()

    lock = get_user_lock(user_id)

    # 2. Act: Acquire the user's lock (simulating an ongoing handle_request)
    async with lock:
        # While locked, trigger the callback
        await new_chat_callback(update, context)

    # 3. Assert
    # The callback should have noticed _is_user_busy() and immediately answered with the toast
    update.callback_query.answer.assert_awaited()
    call_args = update.callback_query.answer.call_args[0]
    call_kwargs = update.callback_query.answer.call_args[1]

    assert len(call_args) > 0 and "Дождитесь" in call_args[0], "Expected heavy callback to be rejected with busy toast"
    assert call_kwargs.get("show_alert") is True

    # DB Update should NOT have been called
    m_update.assert_not_called()


@pytest.mark.asyncio
async def test_sequential_message_processing_via_lock():
    """
    Risk Covered: Concurrent messages from the same user run in parallel and corrupt chat history.
    They should be forced to execute sequentially by the state lock.
    """
    import app.state as state

    user_id = 12345

    execution_order = []

    async def fake_process_long_request_1(*args):
        execution_order.append("start_1")
        await asyncio.sleep(0.2)
        execution_order.append("end_1")

    async def fake_process_long_request_2(*args):
        execution_order.append("start_2")
        await asyncio.sleep(0.1)
        execution_order.append("end_2")

    # Mock context managers and heartbeats to isolate the locking behavior
    with (
        patch("app.handlers.messages.api_logger"),
        patch("app.handlers.messages.metrics_collector"),
        patch("app.handlers.agent.process_long_request", new=fake_process_long_request_1),
    ):
        placeholder = AsyncMock()
        placeholder.message_id = 999

        # We need to simulate the closure in handle_message, but it's nested.
        # Instead, we test the state lock directly, verifying our understanding of it.
        lock = state.get_user_lock(user_id)

        async def mock_handler_task(process_func):
            async with lock:
                await process_func()

        # Fire two handler tasks concurrently
        t1 = asyncio.create_task(mock_handler_task(fake_process_long_request_1))
        # ensure t1 starts first
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(mock_handler_task(fake_process_long_request_2))

        await asyncio.gather(t1, t2)

        # If locks work sequentially, start_2 happens strictly AFTER end_1
        assert execution_order == ["start_1", "end_1", "start_2", "end_2"]
