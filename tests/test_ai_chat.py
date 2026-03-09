"""Tests for app.handlers.ai_chat — regular conversational AI chat."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.handlers.ai_chat import _handle_regular_chat
from tests.factories import make_chat_state, make_telegram_message


@pytest.fixture
def mock_boundaries():
    """Setup clean external boundaries for AI Chat handler without over-mocking internals."""
    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock) as m_resolve,
        # We mock stream_and_display to avoid real AI calls
        patch("app.streaming.stream_and_display", new_callable=AsyncMock) as m_get_resp,
        patch("app.handlers.ai_chat.send_long_message", new_callable=AsyncMock) as m_send_long,
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as m_update_chat,
        # Force non-streaming for deterministic simple tests
        patch("app.handlers.ai_chat.is_openrouter_model", return_value=True),
        # Suppress background DB saving
        patch("app.repos.memory.search_memories", new_callable=AsyncMock) as m_search,
        patch("app.repos.memory.store_memory", new_callable=AsyncMock),
        patch("app.metrics.role_conv_metrics.record_summarization", new_callable=AsyncMock),
    ):
        # Default Arrange values
        m_resolve.return_value = ({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")
        m_get_resp.return_value = ("Hello world!", True, None)
        m_search.return_value = []

        yield {
            "resolve": m_resolve,
            "get_resp": m_get_resp,
            "send_long": m_send_long,
            "update_chat": m_update_chat,
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
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])
    user_message = "Hi"

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, user_message, chat_state)

    # ── Assert ──
    # DB Update checks
    mock_boundaries["update_chat"].assert_awaited_once()
    called_state = mock_boundaries["update_chat"].call_args[0][1]

    assert called_state.token_count == 4, (
        "Expected updated token count based on estimate_tokens_cyrillic('Hello world!')"
    )
    assert any("Hello world!" in str(msg) for msg in called_state.history), "Expected model response in history"
    assert any("Hello world!" in str(msg) for msg in called_state.history), "Expected model response in history"
    # Note: send_long_message is only called if streamed is False.
    # We simulated stream_and_display returning success (True), meaning it streamed.
    # Therefore, we check that it was streamed and edit_reply_markup was called or skipped properly.
    mock_boundaries["send_long"].assert_not_called()


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
    assert "исчерпаны" in edited_text or "лимиты" in edited_text


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
    # Start with length 2 history
    history = [{"role": "user", "parts": ["Hi"]}, {"role": "model", "parts": ["Resp"]}]
    chat_state = make_chat_state(history=history)

    # Simulate an empty response from AI
    mock_boundaries["get_resp"].return_value = (None, False, None)

    # ── Act ──
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # ── Assert ──
    # Verify rollback: last element popped (the history starts at 2, user msg makes it 3, empty response pops it back to 2)
    assert len(chat_state.history) == 2, "Last user message should be popped on empty AI response"
    mock_boundaries["update_chat"].assert_awaited_once_with(user_id, chat_state)

    # Verify user notified
    placeholder.edit_text.assert_awaited()
    assert "пустой ответ" in placeholder.edit_text.call_args_list[-1][0][0].lower()


@pytest.mark.asyncio
async def test_error_response_from_ai(mock_boundaries):
    """
    Risk Covered: Handling standardized AI error strings gracefully.
    Level: Unit.
    """
    # ── Arrange ──
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])
    mock_boundaries["get_resp"].return_value = ("error text mock", False, None)

    with patch("app.handlers.ai_chat.handle_ai_response_error", new_callable=AsyncMock, return_value=True) as m_err:
        # ── Act ──
        await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

        # ── Assert ──
        m_err.assert_awaited()
        # It also must NOT send the error as a regular response
        mock_boundaries["send_long"].assert_not_called()
