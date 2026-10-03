"""End-to-End test for the standard conversational flow.

Checks that the bot can completely process a text message from a user,
hit the AI router, and output a result that persists.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import state
from app.database import db_query
from app.handlers.messages import handle_request
from app.providers.stream_types import (
    FinishReason,
    GroundingReport,
    ProviderKind,
    RouteUsed,
    StreamCompleted,
    TextDelta,
    TokenUsage,
)
from app.utils.background_tasks import TaskManager
from tests.factories import make_telegram_context as make_context
from tests.factories import make_telegram_update as make_update


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_happy_path_conversation(db_conn_with_key, force_test_db_conn):
    """
    Risk Covered: Complete failure of the bot's core pipeline.
    Level: E2E with real chat/quota persistence, stubbed provider SDKs and
    separately tested background memory/transient UI persistence excluded.
    """
    conn, key_hash = db_conn_with_key
    user_id = 999999  # Standard test user ID

    update = make_update(user_id=user_id, message_text="What is the capital of France?")
    context = make_context()

    # Create a proper message mock for the placeholder
    placeholder_msg = make_context().bot.send_message.return_value
    placeholder_msg.edit_text = AsyncMock()
    placeholder_msg.get_bot = MagicMock(return_value=context.bot)
    update.message.reply_text.return_value = placeholder_msg

    # We create a fake async generator for the provider stream so the network isn't hit
    async def fake_stream(request):
        yield TextDelta("The ")
        yield TextDelta("capital ")
        yield TextDelta("is Paris.")
        yield StreamCompleted(
            finish_reason=FinishReason.from_raw("STOP"),
            usage=TokenUsage(total=8),
            grounding=GroundingReport(),
            route=RouteUsed(
                provider=ProviderKind.GEMINI,
                requested_model=request.models[0],
                actual_model=request.models[0],
            ),
        )

    fake_router = AsyncMock()
    fake_router.stream = fake_stream
    memory_client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content=AsyncMock(return_value=SimpleNamespace(text="capital of France Paris")),
                embed_content=AsyncMock(
                    return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0 / (768**0.5)] * 768)])
                ),
            )
        )
    )

    # Use a real local semaphore to perfectly mimic the target without Redis requirements
    local_sem = asyncio.Semaphore(1)
    task_manager = TaskManager()

    # Bound every request-owned task to this test's lifetime and keep specialized
    # SDK calls offline without bypassing the real quota reservation.
    with (
        patch("app.providers.get_provider_router", return_value=fake_router),
        patch("app.repos.memory.get_cached_genai_client", return_value=memory_client),
        patch("app.handlers.messages.submit_task", task_manager.submit),
        patch("app.repos.memory_autosave.submit_memory_task"),
        patch("app.state._schedule_persist"),
        patch("app.adapters.concurrency.heavy_request_semaphore", local_sem),
        patch("app.adapters.concurrency.ultra_heavy_semaphore", local_sem),
    ):
        try:
            await handle_request(update, context)
            ai_task = state._ACTIVE_TASKS.get(user_id)
            assert ai_task is not None
            await ai_task
        finally:
            assert await task_manager.drain(timeout=0, cancel_timeout=1)
            assert not task_manager._tasks

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

    # The specialized memory SDK paths stay offline while their real DB quota
    # reservation remains part of this end-to-end check.
    memory_client.aio.models.generate_content.assert_awaited_once()
    memory_client.aio.models.embed_content.assert_awaited()
    from app.repos.memory import QUERY_EXPANSION_MODEL

    assert (
        await conn.fetchval(
            "SELECT request_count FROM key_usage WHERE key_hash = $1 AND model_name = $2",
            key_hash,
            QUERY_EXPANSION_MODEL,
        )
        == 1
    )
