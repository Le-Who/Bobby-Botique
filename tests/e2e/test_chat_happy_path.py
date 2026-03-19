"""End-to-End test for the standard conversational flow.

Checks that the bot can completely process a text message from a user,
hit the AI router, and output a result that persists.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We only need minimal imports, mostly the entrypoint
from telegram import Update

from app.database import db_query
from app.handlers.messages import handle_request
from tests.factories import make_telegram_context as make_context
from tests.factories import make_telegram_update as make_update

pytest_plugins = ["tests.integration.conftest"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_happy_path_conversation(db_conn_with_key):
    """
    Risk Covered: Complete failure of the bot's core pipeline.
    Level: E2E (With only external AI network mocked out via a stub stream)
    """
    conn, api_key = db_conn_with_key
    user_id = 999999  # Standard test user ID

    update = make_update(user_id=user_id, message_text="What is the capital of France?")
    context = make_context()

    # Create a proper message mock for the placeholder to avoid AsyncMock returning
    # unawaited coroutines for synchronous ptb methods like get_bot()
    placeholder_msg = make_context().bot.send_message.return_value
    placeholder_msg.edit_text = AsyncMock()
    placeholder_msg.get_bot = MagicMock(return_value=context.bot)
    update.message.reply_text.return_value = placeholder_msg

    # We create a fake async generator for the provider stream so the network isn't hit
    async def fake_stream(*args, **kwargs):
        yield "The "
        yield "capital "
        yield "is Paris."

    fake_router = AsyncMock()
    fake_router.stream_response = fake_stream

    # We patch only the external LLM call and the background task dispatcher
    # (Background tasks are tricky to test in pytest-asyncio if they linger)
    with (
        patch("app.providers.get_provider_router", return_value=fake_router),
        patch("app.utils.background_tasks.TaskManager._tasks", set()),
        patch("app.utils.background_tasks.submit_task") as mock_submit,
    ):
        # Execute the incoming update as the bot would
        await handle_request(update, context)

        # In a real run, messages.py triggers the long request task in the background.
        # Let's fish it out and execute it synchronously to complete the test
        assert mock_submit.call_count >= 1
        long_request_coro = mock_submit.call_args_list[0][0][0]

        # Because we're inside db_conn_with_key transaction, we need the background
        # task to use OUR transaction rather than pulling a new pool connection.
        with patch("app.database.db_manager.pool.acquire") as mock_acquire:
            mock_acquire.return_value.__aenter__.return_value = conn
            mock_acquire.return_value.__aexit__.return_value = None

            await long_request_coro

    # Verify the final state in the Database
    # We expect 2 messages in the history
    # We expect 2 messages in the history
    rows = await db_query(
        "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC",
        (user_id,),
        conn=conn,
    )
    assert len(rows) == 2

    # User message
    assert rows[0]["role"] == "user"
    assert "France" in rows[0]["content"]

    # Model message
    assert rows[-1]["role"] == "model"
    assert "Paris" in rows[-1]["content"]

    # Ensure UI was updated
    placeholder_msg = update.message.reply_text.return_value
    assert placeholder_msg.edit_text.call_count >= 1
