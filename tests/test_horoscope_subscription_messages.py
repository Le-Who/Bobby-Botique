from types import SimpleNamespace

import pytest

from app.handlers.horoscope_subscription import _horoscope_invite_text, send_horoscope_invite


def test_horoscope_invite_text_is_clear_opt_in_copy():
    text = _horoscope_invite_text()

    assert "утром" in text.lower()
    assert "вечером" in text.lower()
    assert "не приговор" in text.lower()
    assert "/horoscope_stop" in text
    assert len(text) < 900


@pytest.mark.asyncio
async def test_send_horoscope_invite_uses_polished_text_and_actions():
    calls = {}

    async def send_message(**kwargs):
        calls.update(kwargs)

    bot = SimpleNamespace(send_message=send_message)

    assert await send_horoscope_invite(bot, 123) is True
    assert calls["chat_id"] == 123
    assert calls["text"] == _horoscope_invite_text()
    labels = [button.text for row in calls["reply_markup"].inline_keyboard for button in row]
    assert labels == ["⭐ Настроить гороскоп", "Не сейчас"]

