import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest


@pytest.mark.asyncio
async def test_new_chat_command():
    # Setup standard mock objects
    update = MagicMock()
    context = MagicMock()

    # We care about update.message.reply_text and update.effective_user.id
    update.effective_user.id = 12345
    update.effective_chat.id = 67890
    update.update_id = 11111
    update.message.reply_text = AsyncMock()

    # Mocking db and request_context inside commands
    with (
        patch("app.handlers.commands.db") as mock_db,
        patch(
            "app.utils.decorators.db.is_authorized", new_callable=AsyncMock
        ) as mock_auth,
        patch("app.handlers.commands.set_request_id") as mock_set_request_id,
    ):
        mock_auth.return_value = True
        mock_chat_state = MagicMock()
        mock_chat_state.search_enabled = False
        mock_chat_state.model = "gemini-pro"
        mock_chat_state.system_prompt = "Old prompt"
        mock_chat_state.history = [{"role": "user", "content": "hello"}]
        mock_chat_state.token_count = 100

        mock_db.get_user_chat = AsyncMock(return_value=mock_chat_state)
        mock_db.update_user_chat = AsyncMock()

        from app.handlers.commands import new_chat_command

        await new_chat_command(update, context)

        # Verify side effects
        mock_set_request_id.assert_called_once()
        mock_db.get_user_chat.assert_called_once_with(12345)
        mock_db.update_user_chat.assert_called_once_with(12345, mock_chat_state)

        # Verify state was cleared
        assert mock_chat_state.history == []
        assert mock_chat_state.token_count == 0
        assert mock_chat_state.system_prompt is None

        # Verify user was notified
        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        assert "Новый чат" in args[0]
