from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers import scheduled_horoscopes


@pytest.mark.asyncio
async def test_scheduler_respects_admin_horoscope_delivery_switch(monkeypatch):
    monkeypatch.setattr(
        "app.repos.settings_repo.get_global_setting",
        AsyncMock(return_value="off"),
    )

    with patch.object(
        scheduled_horoscopes,
        "get_due_horoscope_subscriptions",
        new_callable=AsyncMock,
    ) as due_mock:
        await scheduled_horoscopes.check_and_send_horoscopes(SimpleNamespace(bot=object()))

    due_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deliver_horoscope_includes_manage_and_stop_actions(monkeypatch):
    monkeypatch.setattr(
        "app.intent_router._handle_horoscope",
        AsyncMock(return_value=SimpleNamespace(text="**Фокус дня**\n\nВыберите главное.")),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    sent = await scheduled_horoscopes._deliver_horoscope(bot, 42, "aries", "today")

    assert sent is True
    keyboard = bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard
    labels = [button.text for row in keyboard for button in row]
    assert labels == ["⚙️ Настройки", "Отключить"]
