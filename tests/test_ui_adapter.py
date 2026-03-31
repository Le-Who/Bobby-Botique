"""Tests for app.adapters.ui_adapter — TelegramMessageAdapter behavior."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.ui_adapter import TelegramMessageAdapter


@pytest.fixture
def mock_message():
    msg = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.delete = AsyncMock()
    msg.message_id = 42
    return msg


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.send_message_draft = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def adapter(mock_message, mock_bot):
    return TelegramMessageAdapter(message=mock_message, bot=mock_bot, chat_id=123, draft_id=456)


class TestTelegramMessageAdapter:
    """TelegramMessageAdapter wraps python-telegram-bot message operations."""

    @pytest.mark.asyncio
    async def test_edit_message(self, adapter, mock_message):
        await adapter.edit_message("Hello", "HTML")
        mock_message.edit_text.assert_called_once_with("Hello", parse_mode="HTML")

    @pytest.mark.asyncio
    async def test_edit_message_suppresses_not_modified(self, adapter, mock_message):
        from telegram.error import TelegramError

        mock_message.edit_text.side_effect = TelegramError("Message is not modified")
        # Should not raise
        await adapter.edit_message("Hello", "HTML")

    @pytest.mark.asyncio
    async def test_edit_message_raises_other_errors(self, adapter, mock_message):
        from telegram.error import TelegramError

        mock_message.edit_text.side_effect = TelegramError("Something else went wrong")
        with pytest.raises(TelegramError):
            await adapter.edit_message("Hello", "HTML")

    @pytest.mark.asyncio
    async def test_reply_new_message(self, adapter, mock_message):
        new_msg = AsyncMock()
        mock_message.reply_text.return_value = new_msg

        new_adapter = await adapter.reply_new_message("continuation", "HTML")
        mock_message.reply_text.assert_called_once_with(
            "continuation", parse_mode="HTML", allow_sending_without_reply=True
        )
        assert isinstance(new_adapter, TelegramMessageAdapter)

    @pytest.mark.asyncio
    async def test_reply_new_message_fallback_when_deleted(self, adapter, mock_message, mock_bot):
        from telegram.error import TelegramError

        # Simulate original message being deleted
        mock_message.reply_text.side_effect = TelegramError("Message to be replied not found")

        fallback_msg = AsyncMock()
        mock_bot.send_message.return_value = fallback_msg

        new_adapter = await adapter.reply_new_message("fallback", "HTML")

        # Should catch the error and fall back to sending a new message
        mock_bot.send_message.assert_called_once_with(chat_id=123, text="fallback", parse_mode="HTML")

        assert isinstance(new_adapter, TelegramMessageAdapter)
        assert new_adapter.last_message is fallback_msg
