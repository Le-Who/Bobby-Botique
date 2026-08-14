"""Tests for app.handlers.ai_photo — single photo processing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def make_placeholder():
    msg = MagicMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.chat = MagicMock()
    msg.chat.id = 456
    return msg


def make_original_message(caption=None, user_id=123):
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.caption = caption

    # Build photo mock: photo[-1].get_file() -> download_as_bytearray()
    photo_file = MagicMock()
    photo_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\x89PNG"))
    photo_file.get_file = AsyncMock(return_value=photo_file)

    photo_obj = MagicMock()
    photo_obj.get_file = AsyncMock(return_value=photo_file)
    msg.photo = [photo_obj]

    msg.reply_text = AsyncMock()
    return msg


def make_chat_state():
    return SimpleNamespace(
        model="gemini-3.1-flash-lite",
        system_prompt=None,
        history=[],
        token_count=0,
        is_deep_dive=False,
        search_enabled=False,
        thinking_level=None,
        context_summary=None,
        ltm_enabled=False,
    )


# ── Happy path — AI describes photo ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_success():
    """Successfully processes a photo and sends AI description."""
    placeholder = make_placeholder()
    original = make_original_message(caption="What is this?")
    chat_state = make_chat_state()
    stream_message = MagicMock()
    stream_message.edit_reply_markup = AsyncMock()

    with (
        patch("app.handlers.ai_photo.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_photo.get_file_bytes", new_callable=AsyncMock, return_value=b"image-bytes"),
        patch("app.handlers.ai_photo.save_image_as_bytes", new_callable=AsyncMock, return_value=b"compressed"),
        patch(
            "app.handlers.ai_photo.stream_and_display",
            new_callable=AsyncMock,
            return_value=("This is a mountain landscape.", True, stream_message, 0, False, False),
        ) as mock_stream,
        patch(
            "app.handlers.ai_photo._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-3.1-flash-lite", None),
        ),
        patch(
            "app.handlers.ai_photo._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=("This is a mountain landscape.", 10),
        ),
        patch(
            "app.handlers.ai_photo.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_photo.send_long_message", new_callable=AsyncMock) as _mock_send,
        patch("app.handlers.ai_photo.update_user_chat", new_callable=AsyncMock),
    ):
        from app.handlers.ai_photo import _handle_photo

        await _handle_photo(placeholder, original, chat_state)

    # send_long_message is not called when streaming is successful
    # History should be updated
    assert len(chat_state.history) == 2
    assert chat_state.history[1]["role"] == "model"
    reply_markup = mock_stream.await_args.kwargs.get("reply_markup")
    assert reply_markup is not None
    labels = [button.text for row in reply_markup.inline_keyboard for button in row]
    assert labels == ["🎭 Выбрать роль ИИ", "✨ Начать новую тему"]
    stream_message.edit_reply_markup.assert_not_awaited()
    placeholder.edit_text.assert_not_awaited()


# ── Empty AI response ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_empty_response():
    """Shows error when AI returns empty response."""
    placeholder = make_placeholder()
    original = make_original_message()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_photo.update_stage", new_callable=AsyncMock),
        patch(
            "app.handlers.ai_photo.stream_and_display",
            new_callable=AsyncMock,
            return_value=("", False, AsyncMock(), 0, False, False),
        ),
        patch(
            "app.handlers.ai_photo._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=(None, 0),
        ),
        patch(
            "app.handlers.ai_photo._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-3.1-flash-lite", None),
        ),
        patch(
            "app.handlers.ai_photo.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("app.handlers.ai_photo.send_long_message", new_callable=AsyncMock) as mock_send,
    ):
        from app.handlers.ai_photo import _handle_photo

        await _handle_photo(placeholder, original, chat_state)

    mock_send.assert_awaited_once()
    text_arg = mock_send.call_args[0][1]
    assert "не удалось" in text_arg.lower() or "обработать" in text_arg.lower()


# ── AI response is an error ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_ai_error():
    """Handles AI error response without crashing."""
    placeholder = make_placeholder()
    original = make_original_message()
    chat_state = make_chat_state()

    with (
        patch("app.handlers.ai_photo.update_stage", new_callable=AsyncMock),
        patch(
            "app.handlers.ai_photo.stream_and_display",
            new_callable=AsyncMock,
            return_value=("? API Error", False, AsyncMock(), 0, False, False),
        ),
        patch(
            "app.handlers.ai_photo._get_ai_response_with_routing",
            new_callable=AsyncMock,
            return_value=("❌ API Error", 0),
        ),
        patch(
            "app.handlers.ai_photo._resolve_ai_request",
            new_callable=AsyncMock,
            return_value=({"key": "val"}, "gemini-3.1-flash-lite", None),
        ),
        patch(
            "app.handlers.ai_photo.handle_ai_response_error",
            new_callable=AsyncMock,
            return_value=True,
        ),  # Error was handled
    ):
        from app.handlers.ai_photo import _handle_photo

        await _handle_photo(placeholder, original, chat_state)

    # Should not crash, error handled by handle_ai_response_error
    assert len(chat_state.history) == 0  # No history update on error


# ── Exception during download ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_download_exception():
    """Handles exception during photo download gracefully."""
    placeholder = make_placeholder()
    original = make_original_message()
    chat_state = make_chat_state()

    # Make photo download fail
    original.photo[-1].get_file = AsyncMock(side_effect=Exception("Network error"))

    from app.handlers.ai_photo import _handle_photo

    await _handle_photo(placeholder, original, chat_state)

    placeholder.edit_text.assert_awaited_once()
    text = placeholder.edit_text.call_args[0][0]
    assert "ошибка" in text.lower()


# ── Exception during processing with edit fallback ────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_exception_edit_fallback():
    """Falls back to reply_text when edit_text fails during error."""
    placeholder = make_placeholder()
    original = make_original_message()
    chat_state = make_chat_state()

    # Make photo download fail
    original.photo[-1].get_file = AsyncMock(side_effect=Exception("Download failed"))
    # Make edit_text also fail
    placeholder.edit_text = AsyncMock(side_effect=Exception("Message not found"))

    from app.handlers.ai_photo import _handle_photo

    await _handle_photo(placeholder, original, chat_state)

    # Should fall back to reply_text
    original.reply_text.assert_awaited_once()
    text = original.reply_text.call_args[0][0]
    assert "ошибка" in text.lower()
