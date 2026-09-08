from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler

from app.handlers import horoscope_subscription as horoscope


def make_update(entrypoint):
    messages = []

    async def reply_text(text, **kwargs):
        messages.append({"text": text, **kwargs})

    message = SimpleNamespace(reply_text=reply_text)
    query = None
    if entrypoint in ("start_horoscope", "horo_settings:start"):
        query = SimpleNamespace(data=entrypoint, answer=AsyncMock(), edit_message_text=reply_text)
    update = SimpleNamespace(
        effective_message=message,
        message=None if query else message,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        callback_query=query,
    )
    context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=reply_text))
    if entrypoint == "deep_link":
        context.user_data["horo_payload"] = "subscribe_horoscope_leo"
    return update, context, messages


def entry_callback(entrypoint):
    if entrypoint == "/horoscope":
        handler = horoscope.build_horoscope_subscription_handler()
        commands = [
            item for item in handler.entry_points if isinstance(item, CommandHandler) and "horoscope" in item.commands
        ]
        assert len(commands) == 1, "/horoscope must be registered as a subscription entrypoint"
        return commands[0].callback
    if entrypoint == "horo_settings:start":
        return horoscope.horoscope_settings_callback
    return horoscope.start_subscribe_horoscope


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint", ["/horoscope", "гороскоп", "start_horoscope", "horo_settings:start", "deep_link"]
)
@pytest.mark.parametrize("active", [True, False])
async def test_existing_subscription_entrypoints_show_management_not_setup(monkeypatch, entrypoint, active):
    subscription = {
        "user_id": 42,
        "sign": "aries",
        "time_today": time(8, 30),
        "time_tomorrow": None,
        "utc_offset": 2,
        "is_active": active,
    }
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value=subscription))
    update, context, messages = make_update(entrypoint)
    original_user_data = dict(context.user_data)

    state = await entry_callback(entrypoint)(update, context)

    assert state == ConversationHandler.END
    assert len(messages) == 1
    assert "♈ Овен" in messages[0]["text"]
    assert "08:30" in messages[0]["text"]
    assert ("Активна" if active else "Приостановлена") in messages[0]["text"]
    buttons = [button for row in messages[0]["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        "horo_settings:edit",
        "horo_settings:toggle",
        "horo_settings:delete",
    ]
    assert ("Приостановить" if active else "Возобновить") in buttons[1].text
    assert context.user_data == original_user_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint", ["/horoscope", "гороскоп", "start_horoscope", "horo_settings:start", "deep_link"]
)
async def test_new_subscriber_entrypoints_still_open_sign_selection(monkeypatch, entrypoint):
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value=None))
    update, context, messages = make_update(entrypoint)

    state = await entry_callback(entrypoint)(update, context)

    assert state == horoscope.CHOOSE_SIGN
    buttons = [button for row in messages[0]["reply_markup"].inline_keyboard for button in row]
    assert len(buttons) == 12
    assert all(button.callback_data.startswith("horo_sign:") for button in buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start", "edit"])
async def test_setup_callbacks_enter_conversation_for_followup_sign_selection(monkeypatch, action):
    handler = horoscope.build_horoscope_subscription_handler()
    data = f"horo_settings:{action}"
    entries = [
        item for item in handler.entry_points if isinstance(item, CallbackQueryHandler) and item.pattern.match(data)
    ]
    assert len(entries) == 1, "Setup callbacks must belong to the conversation so their returned state is retained"
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value=None))
    update, context, messages = make_update("horo_settings:start")
    update.callback_query.data = data

    state = await entries[0].callback(update, context)

    assert state == horoscope.CHOOSE_SIGN
    assert state in handler.states
    buttons = [button for row in messages[0]["reply_markup"].inline_keyboard for button in row]
    assert len(buttons) == 12
