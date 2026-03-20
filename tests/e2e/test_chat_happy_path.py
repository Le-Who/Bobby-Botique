"""End-to-End test for the standard conversational flow.

Checks that the bot can completely process a text message from a user,
hit the AI router, and output a result that persists.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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

    # Create a proper message mock for the placeholder
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

    # Use a real local semaphore to perfectly mimic the target without Redis requirements
    local_sem = asyncio.Semaphore(1)

    # We patch only the external LLM call, the background task dispatcher, and semaphores AT SOURCE
    with (
        patch("app.providers.get_provider_router", return_value=fake_router),
        patch("app.utils.background_tasks.TaskManager._tasks", set()),
        patch("app.utils.background_tasks.submit_task") as mock_submit,
        patch("app.adapters.concurrency.heavy_request_semaphore", local_sem),
        patch("app.adapters.concurrency.ultra_heavy_semaphore", local_sem),
    ):
        # Execute the incoming update as the bot would
        await handle_request(update, context)

        # CRITICAL FIX: handle_request launches fire-and-forget background tasks
        # (like _schedule_persist for last_sent_message). We MUST allow the event loop
        # a tiny slice of time to finish those DB inserts before we manually trigger
        # the next stage, otherwise both coroutines fight for the single test DB connection.
        await asyncio.sleep(0.1)

        assert mock_submit.call_count >= 1
        long_request_coro = mock_submit.call_args_list[0][0][0]

        # Execute the background task synchronously to complete the test
        await long_request_coro

    # If the process_long_request loop crashed internally, it will swallow the exception
    # and call edit_text to tell the user an error occurred. Let's catch that explicitly.
    edit_calls = placeholder_msg.edit_text.call_args_list
    if edit_calls:
        last_edit_text = edit_calls[-1][1].get("text") or edit_calls[-1][0][0]
        assert "ошибка" not in last_edit_text.lower() and "error" not in last_edit_text.lower(), (
            f"Test failed internally with swallowed error: {last_edit_text}"
        )

    # Verify the final state in the Database
    # We expect 2 messages in the history
    rows = await db_query(
        "SELECT role, content FROM active_chat_messages WHERE user_id = $1 ORDER BY id ASC",
        (user_id,),
        conn=conn,
    )
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}. DB writes failed."

    # User message
    assert rows[0]["role"] == "user"
    assert "France" in rows[0]["content"]

    # Model message
    assert rows[-1]["role"] == "model"
    assert "Paris" in rows[-1]["content"]

    # Ensure UI was updated
    assert placeholder_msg.edit_text.call_count >= 1
