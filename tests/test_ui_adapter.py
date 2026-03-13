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
    async def test_send_draft(self, adapter, mock_bot):
        await adapter.send_draft("draft text", "HTML")
        mock_bot.send_message_draft.assert_called_once_with(
            chat_id=123, draft_id=456, text="draft text", parse_mode="HTML"
        )

    @pytest.mark.asyncio
    async def test_send_draft_without_bot_raises(self, mock_message):
        adapter_no_bot = TelegramMessageAdapter(message=mock_message)
        with pytest.raises(ValueError, match="bot instance"):
            await adapter_no_bot.send_draft("text", "HTML")

    @pytest.mark.asyncio
    async def test_reply_new_message(self, adapter, mock_message):
        new_msg = AsyncMock()
        mock_message.reply_text.return_value = new_msg

        new_adapter = await adapter.reply_new_message("continuation", "HTML")
        mock_message.reply_text.assert_called_once_with("continuation", parse_mode="HTML")
        assert isinstance(new_adapter, TelegramMessageAdapter)

    @pytest.mark.asyncio
    async def test_delete_placeholder(self, adapter, mock_message):
        await adapter.delete_placeholder()
        mock_message.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_final_message(self, adapter, mock_bot):
        new_msg = AsyncMock()
        mock_bot.send_message.return_value = new_msg

        await adapter.send_final_message("final text", "HTML", reply_markup="keyboard")
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args[1]
        assert call_kwargs["text"] == "final text"
        assert call_kwargs["reply_markup"] == "keyboard"

    def test_last_message(self, adapter, mock_message):
        assert adapter.last_message is mock_message
