"""Tests for app.handlers.ai_chat — regular conversational AI chat."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.handlers.ai_chat import _handle_regular_chat
from tests.factories import make_chat_state, make_telegram_message


@pytest.fixture
def mock_dependencies():
    """Setup all external dependencies needed by AI Chat handler."""
    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock) as m_resolve,
        patch("app.handlers.ai_chat._get_ai_response_with_routing", new_callable=AsyncMock) as m_get_resp,
        patch("app.handlers.ai_chat.update_stage", new_callable=AsyncMock) as m_update_stg,
        patch(
            "app.handlers.ai_chat.handle_ai_response_error", new_callable=AsyncMock, return_value=False
        ) as m_handle_err,
        patch("app.handlers.ai_chat.send_long_message", new_callable=AsyncMock) as m_send_long,
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as m_update_chat,
        patch("app.handlers.ai_chat.get_registry") as m_registry,
        patch("app.handlers.ai_chat.is_openrouter_model", return_value=True),  # Force non-streaming for testing
        patch("app.metrics.role_conv_metrics.record_summarization", new_callable=AsyncMock),
        patch("app.repos.memory.search_memories", new_callable=AsyncMock),
        patch("app.repos.memory.store_memory", new_callable=AsyncMock),
    ):
        # Default successful setup
        m_resolve.return_value = ({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")
        m_get_resp.return_value = ("Hello world!", 42)

        reg_mock = MagicMock()
        reg_mock.compose_system_prompt.return_value = "System directive"
        m_registry.return_value = reg_mock

        yield {
            "resolve": m_resolve,
            "get_resp": m_get_resp,
            "update_stg": m_update_stg,
            "handle_err": m_handle_err,
            "send_long": m_send_long,
            "update_chat": m_update_chat,
        }


@pytest.mark.asyncio
async def test_successful_chat_response_appended_to_history(mock_dependencies):
    """
    Risk Covered: System fails to persist AI reply or token counts.
    Level: Unit.
    """
    # Arrange
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()
    user_message = "Hi"

    # Act
    await _handle_regular_chat(placeholder, user_id, user_message, chat_state)

    # Assert
    assert any("Hello world!" in str(msg) for msg in chat_state.history), "Expected model response in history"
    assert chat_state.token_count == 42, "Expected updated token count"
    mock_dependencies["update_chat"].assert_awaited_once_with(user_id, chat_state)
    mock_dependencies["send_long"].assert_awaited_once()


@pytest.mark.asyncio
async def test_exhausted_limits_shows_error_message(mock_dependencies):
    """
    Risk Covered: System crashes or hangs when API keys are exhausted.
    Level: Unit.
    """
    # Arrange
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()
    mock_dependencies["resolve"].return_value = (None, None, "all_exhausted")

    # Act
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # Assert
    placeholder.edit_text.assert_awaited_once()
    edited_text = placeholder.edit_text.call_args[0][0].lower()
    assert "исчерпаны" in edited_text or "лимиты" in edited_text


@pytest.mark.asyncio
async def test_model_exhausted_prompts_fallback_confirmation(mock_dependencies):
    """
    Risk Covered: Silent failure when switching to fallback model instead of asking user.
    Level: Unit.
    """
    # Arrange
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state()
    mock_dependencies["resolve"].return_value = ({"api_key": "k"}, "gemini-1.5-pro", "confirm_fallback")

    # Act
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # Assert
    placeholder.edit_text.assert_awaited_once()
    call_args, call_kwargs = placeholder.edit_text.call_args
    assert "reply_markup" in call_kwargs, "Expected inline keyboard for fallback confirmation"
    assert "gemini-1.5-pro" in call_args[0]


@pytest.mark.asyncio
async def test_empty_response_rolls_back_history(mock_dependencies):
    """
    Risk Covered: Storing empty AI responses clutters history and causes errors on next turn.
    Level: Unit.
    """
    # Arrange
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    history = [{"role": "user", "parts": ["Hi"]}]
    chat_state = make_chat_state(history=history)
    mock_dependencies["get_resp"].return_value = (None, 0)

    # Act
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # Assert
    assert len(chat_state.history) == 0, "Last user message should be popped on empty AI response"
    mock_dependencies["update_chat"].assert_awaited_once_with(user_id, chat_state)
    placeholder.edit_text.assert_awaited_once()
    assert "пустой ответ" in placeholder.edit_text.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_error_response_triggers_cleanup(mock_dependencies):
    """
    Risk Covered: AI failure states not triggering proper error handler.
    Level: Unit.
    """
    # Arrange
    user_id = 123
    placeholder = make_telegram_message(user_id=user_id)
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])
    mock_dependencies["get_resp"].return_value = ("503 Service Unavailable", 0)
    mock_dependencies["handle_err"].return_value = True

    # Act
    await _handle_regular_chat(placeholder, user_id, "Hi", chat_state)

    # Assert
    mock_dependencies["handle_err"].assert_awaited_once()
