from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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

    mock_chat_state = MagicMock()
    mock_chat_state.search_enabled = False
    mock_chat_state.model = "gemini-pro"
    mock_chat_state.system_prompt = "Old prompt"
    mock_chat_state.history = [{"role": "user", "content": "hello"}]
    mock_chat_state.token_count = 100

    # Mocking direct imports inside commands
    with (
        patch(
            "app.handlers.commands.get_user_chat",
            new_callable=AsyncMock,
            return_value=mock_chat_state,
        ) as mock_get,
        patch("app.handlers.commands.update_user_chat", new_callable=AsyncMock) as mock_update,
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock) as mock_auth,
        patch("app.utils.decorators.set_request_id"),
    ):
        mock_auth.return_value = True

        from app.handlers.commands import new_chat_command

        await new_chat_command(update, context)

        # Verify side effects
        mock_get.assert_called_once_with(12345)
        mock_update.assert_called_once_with(12345, mock_chat_state)

        # Verify state was cleared
        assert mock_chat_state.history == []
        assert mock_chat_state.token_count == 0
        assert mock_chat_state.system_prompt is None

        # Verify user was notified
        update.message.reply_text.assert_called_once()
        args, kwargs = update.message.reply_text.call_args
        assert "Новый чат" in args[0]


@pytest.mark.asyncio
async def test_games_command_private_chat_uses_web_app_button():
    update = MagicMock()
    context = MagicMock()
    context.bot.username = "b0b_bot"
    context.user_data = {}
    update.effective_user.id = 12345
    update.effective_chat.id = 67890
    update.effective_chat.type = "private"
    update.update_id = 22222
    update.message.text = "/games"
    update.message.reply_text = AsyncMock()
    update.callback_query = None

    with (
        patch("app.config.settings", SimpleNamespace(GAME_HUB_URL="https://games.tri.mom")),
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.utils.decorators.set_request_id"),
    ):
        from app.handlers.commands import games_command

        await games_command(update, context)

    update.message.reply_text.assert_awaited_once()
    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.web_app.url == "https://games.tri.mom"
    assert button.url is None


@pytest.mark.asyncio
async def test_games_command_group_chat_uses_direct_link_button():
    update = MagicMock()
    context = MagicMock()
    context.bot.username = "b0b_bot"
    context.user_data = {}
    update.effective_user.id = 12345
    update.effective_chat.id = -1001
    update.effective_chat.type = "supergroup"
    update.update_id = 33333
    update.message.text = "/games"
    update.message.reply_text = AsyncMock()
    update.callback_query = None

    settings = SimpleNamespace(
        GAME_HUB_URL="https://games.tri.mom",
        GAME_HUB_DIRECT_LINK="https://t.me/b0b_bot/games",
        GAME_HUB_MINIAPP_SHORT_NAME="games",
    )
    with (
        patch("app.config.settings", settings),
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.utils.decorators.set_request_id"),
    ):
        from app.handlers.commands import games_command

        await games_command(update, context)

    update.message.reply_text.assert_awaited_once()
    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.url == "https://t.me/b0b_bot/games"
    assert button.web_app is None


@pytest.mark.asyncio
async def test_live_unavailable_message_does_not_expose_server_configuration(monkeypatch):
    update = MagicMock()
    context = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat.id = 67890
    update.update_id = 44444
    update.message.reply_text = AsyncMock()
    monkeypatch.delenv("WEBHOOK_URL", raising=False)

    with (
        patch("app.utils.decorators.is_authorized", new_callable=AsyncMock, return_value=True),
        patch("app.utils.decorators.set_request_id"),
    ):
        from app.handlers.commands import live_command

        await live_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "WEBHOOK_URL" not in text
    assert "HTTPS" not in text
    assert "попробуйте позже" in text.lower()
