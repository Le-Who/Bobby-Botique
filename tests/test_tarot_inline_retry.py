"""
RED tests for _generate_tarot_inline retry/key-rotation behaviour.

Root cause: _generate_tarot_inline used a bespoke single-key call that
re-raised 503 errors directly. It was patched to use _stream_inline_fast,
and then patched again to use ProviderRouter.get_response() to avoid
unnecessary parallel racing for a stable QNA_MODEL.

These tests verify that:
1. _generate_tarot_inline delegates to ProviderRouter.get_response()
2. On total failure (empty or error message), it gracefully edits the message.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_bot(edit_calls: list | None = None) -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=lambda **kw: edit_calls.append(kw) if edit_calls is not None else None)
    return bot

@pytest.mark.asyncio
async def test_generate_tarot_inline_uses_provider_router():
    """_generate_tarot_inline must use ProviderRouter.get_response
    with sequential rotation, instead of _stream_inline_fast or
    bespoke resolve_ai_request calls.
    """
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    # We mock get_provider_router to return a mock router
    mock_router = AsyncMock()
    mock_router.get_response.return_value = ("🔮 Карты говорят: всё будет хорошо.", 100)

    with patch("app.providers.router.get_provider_router", return_value=mock_router):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-1",
            user_query="таро",
            user_id=123,
            spread_type="tarot",
        )

    mock_router.get_response.assert_called_once()
    kwargs = mock_router.get_response.call_args[1]
    assert kwargs.get("max_key_retries") == 3
    assert kwargs.get("preferred_model") == "gemini-3.5-flash"

    # The bot must have edited the message with actual content
    bot.edit_message_text.assert_called()
    final_call_kwargs = bot.edit_message_text.call_args_list[-1][1]
    text = final_call_kwargs.get("text", "")
    assert "❌" not in text

@pytest.mark.asyncio
async def test_generate_tarot_inline_shows_error_on_total_failure():
    """When get_response returns (None, 0) or an error message,
    _generate_tarot_inline must gracefully edit the message with
    an error text and NOT raise an unhandled exception.
    """
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    mock_router = AsyncMock()
    mock_router.get_response.return_value = (None, 0)

    with patch("app.providers.router.get_provider_router", return_value=mock_router):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-3",
            user_query="таро любовь",
            user_id=456,
            spread_type="tarot_love",
        )

    bot.edit_message_text.assert_called()
    final_kwargs = bot.edit_message_text.call_args_list[-1][1]
    error_text = final_kwargs.get("text", "")
    assert "❌" in error_text
    assert "Повтор" in error_text
    assert "друг" in error_text.lower()
    retry_markup = final_kwargs.get("reply_markup")
    assert retry_markup is not None
    retry_button = retry_markup.inline_keyboard[0][0]
    assert retry_button.callback_data.startswith("inl_retry:")

@pytest.mark.asyncio
async def test_generate_tarot_inline_shows_error_on_error_message():
    """When get_response returns an error string (e.g. from ProviderRouter),
    it should also show an error gracefully.
    """
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    mock_router = AsyncMock()
    # is_error_message checks for certain strings, like "Внутренняя ошибка"
    mock_router.get_response.return_value = ("❌ Ошибка при обращении к API", 0)

    with patch("app.providers.router.get_provider_router", return_value=mock_router), \
         patch("app.handlers.inline.is_error_message", return_value=True):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-4",
            user_query="таро",
            user_id=456,
            spread_type="tarot",
        )

    bot.edit_message_text.assert_called()
    error_text = bot.edit_message_text.call_args_list[-1][1].get("text", "")
    assert "❌" in error_text


@pytest.mark.asyncio
async def test_generate_tarot_inline_shows_retry_on_provider_exception():
    """Provider exceptions must not leave the inline placeholder stuck forever."""
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()

    mock_router = AsyncMock()
    mock_router.get_response.side_effect = RuntimeError("provider unavailable")

    with patch("app.providers.router.get_provider_router", return_value=mock_router):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-msg-5",
            user_query="таро",
            user_id=456,
            spread_type="tarot",
        )

    bot.edit_message_text.assert_called()
    final_kwargs = bot.edit_message_text.call_args_list[-1][1]
    error_text = final_kwargs.get("text", "")
    assert "❌" in error_text
    assert "Повтор" in error_text
    retry_markup = final_kwargs.get("reply_markup")
    assert retry_markup is not None
    assert retry_markup.inline_keyboard[0][0].callback_data.startswith("inl_retry:")
