"""Tests for app.handlers.ai_chat — regular conversational AI chat."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_chat_state(history=None, model="gemini-2.0-flash", system_prompt=None,
                    token_count=0, is_deep_dive=False):
    """Create a minimal ChatState-like object."""
    cs = SimpleNamespace(
        history=history if history is not None else [],
        model=model,
        system_prompt=system_prompt,
        token_count=token_count,
        is_deep_dive=is_deep_dive,
        search_enabled=False,
        context_summary=None,
        thinking_level=None,
    )
    return cs


def make_placeholder():
    """Create a mock placeholder message."""
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.chat.id = 456
    msg.from_user.id = 123
    return msg


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_regular_chat_success():
    """Successful AI response appends to history and persists."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock,
              return_value=({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")),
        patch("app.handlers.ai_chat._get_ai_response_with_routing", new_callable=AsyncMock,
              return_value=("Hello world!", 42)),
        patch("app.handlers.ai_chat.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_chat.handle_ai_response_error", new_callable=AsyncMock,
              return_value=False),
        patch("app.handlers.ai_chat.send_long_message", new_callable=AsyncMock),
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as mock_save,
        patch("app.handlers.ai_chat.prompts") as mock_prompts,
    ):
        mock_prompts.prepare_context_with_limits.return_value = ([], None)
        mock_prompts.build_context_with_summary.return_value = []
        mock_prompts.compose_system_instruction.return_value = "sys"

        from app.handlers.ai_chat import _handle_regular_chat
        await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    # History should have the model response appended
    assert any("Hello world!" in str(h) for h in chat_state.history)
    assert chat_state.token_count == 42
    mock_save.assert_awaited_once()


# ── All limits exhausted ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_regular_chat_all_exhausted():
    """When all API keys exhausted, shows limit message."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock,
              return_value=(None, None, "all_exhausted")),
    ):
        from app.handlers.ai_chat import _handle_regular_chat
        await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    placeholder.edit_text.assert_awaited_once()
    text = placeholder.edit_text.call_args[0][0]
    assert "лимиты" in text.lower() or "исчерпаны" in text.lower()


# ── Fallback confirmation ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_regular_chat_confirm_fallback():
    """When model exhausted, offers fallback model confirmation."""
    placeholder = make_placeholder()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock,
              return_value=({"api_key": "k"}, "gemini-1.5-pro", "confirm_fallback")),
    ):
        from app.handlers.ai_chat import _handle_regular_chat
        await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    placeholder.edit_text.assert_awaited_once()
    call_kwargs = placeholder.edit_text.call_args
    assert "reply_markup" in call_kwargs[1]
    text = call_kwargs[0][0]
    assert "gemini-1.5-pro" in text


# ── Empty response ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_regular_chat_empty_response():
    """Empty AI response pops last history entry and persists."""
    placeholder = make_placeholder()
    history = [{"role": "user", "parts": ["Hi"]}]
    chat_state = make_chat_state(history=history)

    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock,
              return_value=({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")),
        patch("app.handlers.ai_chat._get_ai_response_with_routing", new_callable=AsyncMock,
              return_value=(None, 0)),
        patch("app.handlers.ai_chat.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock) as mock_save,
        patch("app.handlers.ai_chat.prompts") as mock_prompts,
    ):
        mock_prompts.prepare_context_with_limits.return_value = (history, None)
        mock_prompts.build_context_with_summary.return_value = history
        mock_prompts.compose_system_instruction.return_value = "sys"

        from app.handlers.ai_chat import _handle_regular_chat
        await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    # State should have been saved (history popped)
    mock_save.assert_awaited_once()


# ── Error response triggers cleanup ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_regular_chat_error_response_cleanup():
    """AI error response triggers cleanup callback."""
    placeholder = make_placeholder()
    chat_state = make_chat_state(history=[{"role": "user", "parts": ["Hi"]}])

    with (
        patch("app.handlers.ai_chat._resolve_ai_request", new_callable=AsyncMock,
              return_value=({"api_key": "k", "key_hash": "h"}, "gemini-2.0-flash", "direct")),
        patch("app.handlers.ai_chat._get_ai_response_with_routing", new_callable=AsyncMock,
              return_value=("503 Service Unavailable", 0)),
        patch("app.handlers.ai_chat.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_chat.handle_ai_response_error", new_callable=AsyncMock,
              return_value=True) as mock_handle_err,
        patch("app.handlers.ai_chat.update_user_chat", new_callable=AsyncMock),
        patch("app.handlers.ai_chat.prompts") as mock_prompts,
    ):
        mock_prompts.prepare_context_with_limits.return_value = ([], None)
        mock_prompts.build_context_with_summary.return_value = []
        mock_prompts.compose_system_instruction.return_value = "sys"

        from app.handlers.ai_chat import _handle_regular_chat
        await _handle_regular_chat(placeholder, 123, "Hi", chat_state)

    # Error handler should have been invoked
    mock_handle_err.assert_awaited_once()
