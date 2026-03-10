"""Tests for app.handlers.ai_chat — regular conversational AI chat."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Message

from app.handlers.ai_chat import _handle_regular_chat
from tests.factories import make_chat_state, make_telegram_message


@pytest.fixture
def mock_boundaries():
    """Setup external boundaries for AI Chat handler with strict AAA isolation."""

    # We create a fake async generator for the provider stream
    async def fake_stream(*args, **kwargs):
        yield "Hello "
        yield "world!"

    # Create a mock router that returns our fake stream
    fake_router = MagicMock()
    fake_router.stream_response = fake_stream

    with (
        # 1. Key resolution (independent domain, mocked)
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock) as m_resolve,
        # 2. Provider router (the actual external boundary, mocked instead of stream_and_display)
        patch("app.providers.get_provider_router", return_value=fake_router),
        # 3. DB persistence
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as m_update_chat,
        # 4. Long-term memory search (independent domain)
        patch("app.repos.memory.search_memories", new_callable=AsyncMock, return_value=[]),
        patch("app.streaming.metrics_collector", MagicMock(record_api_call=AsyncMock())),
        patch("app.metrics.role_conv_metrics.record_summarization", new_callable=AsyncMock),
        patch("app.handlers.ai_chat.update_stage", new_callable=AsyncMock),
    ):
        m_resolve.return_value = ({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")

        yield {
            "resolve": m_resolve,
            "update_chat": m_update_chat,
            "router": fake_router,
        }


@pytest.mark.asyncio
async def test_successful_chat_response_appended_to_history(mock_boundaries):
    """
    Risk Covered: System fails to persist AI reply or token counts.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    placeholder.chat.type = "private"
    placeholder.get_bot = MagicMock(return_value=None)

    # Pre-existing chat state
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    mock_boundaries["update_chat"].assert_awaited_once()
    saved_state = mock_boundaries["update_chat"].call_args[0][1]

    # Verify Behavior: The generated response is appended to history
    assert len(saved_state.history) == 2, "Expected 1 new message in history"
    assert saved_state.history[-1]["role"] == "model"
    assert "Hello world!" in saved_state.history[-1]["parts"][0]

    # Verify Behavior: Token limit correctly updated internally
    assert saved_state.token_count > 0, "Expected updated token count based on the assembled chunk"


@pytest.mark.asyncio
async def test_exhausted_limits_shows_error_message(mock_boundaries):
    """
    Risk Covered: System crashes or hangs when API keys are exhausted.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()
    mock_boundaries["resolve"].return_value = (None, None, "all_exhausted")

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    placeholder.edit_text.assert_awaited_once()
    edited_text = placeholder.edit_text.call_args[0][0].lower()
    assert "исчерпаны" in edited_text or "лимит" in edited_text


@pytest.mark.asyncio
async def test_model_exhausted_prompts_fallback_confirmation(mock_boundaries):
    """
    Risk Covered: Silent failure when switching to fallback model instead of asking user.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()
    mock_boundaries["resolve"].return_value = ({"api_key": "fixed_key"}, "gemini-1.5-pro", "confirm_fallback")

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    placeholder.edit_text.assert_awaited_once()
    call_args, call_kwargs = placeholder.edit_text.call_args
    assert "reply_markup" in call_kwargs, "Expected inline keyboard for fallback confirmation"
    assert "gemini-1.5-pro" in call_args[0], "Expected fallback model name in prompt"


@pytest.mark.asyncio
async def test_empty_response_rolls_back_history(mock_boundaries):
    """
    Risk Covered: Storing empty AI responses clutters history and causes errors on next turn.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    placeholder.chat.type = "private"
    placeholder.get_bot = MagicMock(return_value=None)

    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}, {"role": "model", "parts": ["Resp"]}])

    # Simulate an empty response via the stream
    async def empty_stream(*args, **kwargs):
        if False:
            yield ""  # enforce generator

    mock_boundaries["router"].stream_response = empty_stream

    with patch("app.errors.build_retry_and_roles_keyboard", return_value=None):
        # ── Act ──
        await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    # Rollback: user message was appended internally during Context Assembler, but should be popped back
    # to original length of 2
    mock_boundaries["update_chat"].assert_awaited_once_with(user_id, chat_state)
    assert len(chat_state.history) == 2, "Last user message should be popped on empty AI response"

    placeholder.edit_text.assert_awaited()
    assert "пустой ответ" in placeholder.edit_text.call_args_list[-1][0][0].lower()
