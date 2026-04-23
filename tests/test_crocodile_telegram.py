from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.games import crocodile_telegram


@pytest.mark.asyncio
async def test_send_thermometer_update_keeps_play_button(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MINIAPP_SHORT_NAME", "miniapp", raising=False)
    monkeypatch.setattr(settings, "WEBAPP_BASE_URL", "https://example.com", raising=False)
    bot = SimpleNamespace(username="testbot", edit_message_text=AsyncMock())
    game = SimpleNamespace(
        game_id="game-123",
        inline_message_id="inline-123",
        best_score=0.74,
    )

    await crocodile_telegram.CrocodileTelegramService.send_thermometer_update(bot, game)

    kwargs = bot.edit_message_text.await_args.kwargs
    keyboard = kwargs["reply_markup"]
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/testbot/miniapp?startapp=game-123"


@pytest.mark.asyncio
async def test_flush_thermometer_update_keeps_play_button(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MINIAPP_SHORT_NAME", "miniapp", raising=False)
    monkeypatch.setattr(settings, "WEBAPP_BASE_URL", "https://example.com", raising=False)
    bot = SimpleNamespace(username="testbot", edit_message_text=AsyncMock())
    crocodile_telegram._pending_thermometer_updates["inline-456"] = {
        "bot": bot,
        "game_id": "game-456",
        "inline_message_id": "inline-456",
        "best_score": 0.51,
    }

    with patch("app.games.crocodile_telegram.asyncio.sleep", new_callable=AsyncMock):
        await crocodile_telegram.CrocodileTelegramService._flush_thermometer_update("inline-456")

    kwargs = bot.edit_message_text.await_args.kwargs
    keyboard = kwargs["reply_markup"]
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/testbot/miniapp?startapp=game-456"
