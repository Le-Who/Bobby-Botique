"""Tests for app.handlers.ai_photo — single photo processing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.errors import ErrorCode
from app.response_delivery.outcomes import CompleteDelivery, FailedDelivery
from app.response_delivery.renderer import (
    DeliveryKind,
    DeliveryReceipt,
    TelegramMessageRef,
)


def _receipt() -> DeliveryReceipt:
    return DeliveryReceipt(
        kind=DeliveryKind.MESSAGE,
        message_ids=(1,),
        final_message=TelegramMessageRef(chat_id=456, message_id=1),
    )


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
    """Successfully processes a photo through typed response delivery."""
    placeholder = make_placeholder()
    original = make_original_message(caption="What is this?")
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=CompleteDelivery(
            content_text="This is a mountain landscape.",
            displayed_text="This is a mountain landscape.",
            completion=None,
            voice_requested=False,
            receipt=_receipt(),
        )
    )

    with (
        patch("app.handlers.ai_photo.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_photo.get_file_bytes", new_callable=AsyncMock, return_value=b"image-bytes"),
        patch("app.handlers.ai_photo.save_image_as_bytes", new_callable=AsyncMock, return_value=b"compressed"),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
        patch("app.handlers.ai_photo.update_user_chat", new_callable=AsyncMock),
    ):
        from app.handlers.ai_photo import _handle_photo

        await _handle_photo(placeholder, original, chat_state)

    assert len(chat_state.history) == 2
    assert chat_state.history[1]["role"] == "model"
    request = delivery.stream.await_args.args[1]
    assert request.turns[0].parts[1].data == b"compressed"
    presentation = delivery.stream.await_args.kwargs["presentation"]
    labels = [
        button.text
        for row in presentation.actions.inline_keyboard
        for button in row
    ]
    assert labels == ["🎭 Выбрать роль ИИ", "✨ Начать новую тему"]


# ── Empty AI response ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_empty_response():
    """Typed delivery renders empty-response failure exactly once."""
    placeholder = make_placeholder()
    original = make_original_message()
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=FailedDelivery(
            error_code=ErrorCode.EMPTY_RESPONSE,
            displayed_text="Пустой ответ",
            receipt=_receipt(),
        )
    )

    with (
        patch("app.handlers.ai_photo.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_photo.get_file_bytes", new_callable=AsyncMock, return_value=b"image"),
        patch("app.handlers.ai_photo.save_image_as_bytes", new_callable=AsyncMock, return_value=b"compressed"),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
    ):
        from app.handlers.ai_photo import _handle_photo

        await _handle_photo(placeholder, original, chat_state)

    assert chat_state.history == []


# ── AI response is an error ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_photo_ai_error():
    """Typed provider failure is already displayed and does not mutate history."""
    placeholder = make_placeholder()
    original = make_original_message()
    chat_state = make_chat_state()
    delivery = MagicMock()
    delivery.stream = AsyncMock(
        return_value=FailedDelivery(
            error_code=ErrorCode.KEYS_EXHAUSTED,
            displayed_text="API error",
            receipt=_receipt(),
        )
    )

    with (
        patch("app.handlers.ai_photo.update_stage", new_callable=AsyncMock),
        patch("app.handlers.ai_photo.get_file_bytes", new_callable=AsyncMock, return_value=b"image"),
        patch("app.handlers.ai_photo.save_image_as_bytes", new_callable=AsyncMock, return_value=b"compressed"),
        patch(
            "app.response_delivery.delivery.get_telegram_response_delivery",
            return_value=delivery,
        ),
    ):
        from app.handlers.ai_photo import _handle_photo

        await _handle_photo(placeholder, original, chat_state)

    assert len(chat_state.history) == 0


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
