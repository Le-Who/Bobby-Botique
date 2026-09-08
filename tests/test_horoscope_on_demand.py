import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import CommandHandler, MessageHandler

from app.handlers import horoscope_subscription as horoscope


def request(data="horo_settings:now:today"):
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_message=message,
        message=message,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
    )
    return update, SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", [None, {"sign": "leo", "is_active": False}])
async def test_horoscope_commands_and_text_aliases_offer_immediate_reading(monkeypatch, sub):
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value=sub))
    handler = horoscope.build_horoscope_subscription_handler()
    entries = [entry for entry in handler.entry_points if isinstance(entry, CommandHandler | MessageHandler)]
    for entry in entries:
        update, context = request()
        update.callback_query = None
        await entry.callback(update, context)
        keyboard = update.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        actions = {button.callback_data for row in keyboard for button in row}
        assert {"horo_settings:now:today", "horo_settings:now:tomorrow"} <= actions


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("kind", ["today", "tomorrow"])
async def test_manual_reading_uses_subscription_without_changing_delivery(monkeypatch, active, kind):
    monkeypatch.setattr(
        horoscope, "get_horoscope_subscription", AsyncMock(return_value={"sign": "leo", "is_active": active})
    )
    writes = AsyncMock(side_effect=AssertionError("manual reading must not update subscription"))
    monkeypatch.setattr(horoscope, "upsert_horoscope_subscription", writes)
    monkeypatch.setattr("app.handlers.scheduled_horoscopes.mark_horoscope_sent", writes)
    monkeypatch.setattr(
        "app.intent_router._handle_horoscope", AsyncMock(return_value=SimpleNamespace(text="Прогноз готов"))
    )
    update, context = request(f"horo_settings:now:{kind}")
    context.user_data["horo_sign"] = "aries"  # An unrelated unfinished setup draft.
    await horoscope.horoscope_settings_callback(update, context)
    sent = context.bot.send_message.await_args
    assert sent is not None
    assert sent.kwargs["chat_id"] == 42
    assert "Лев" in sent.kwargs["text"]
    assert ("сегодня" if kind == "today" else "завтра") in sent.kwargs["text"]
    assert context.user_data["horo_sign"] == "aries"
    writes.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_subscription_choose_sign_for_one_off_without_wizard(monkeypatch):
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value=None))
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr("app.handlers.scheduled_horoscopes._deliver_horoscope", deliver)
    update, context = request("horo_settings:now:tomorrow")
    await horoscope.horoscope_settings_callback(update, context)
    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    buttons = [button for row in keyboard for button in row]
    assert len(buttons) == 12
    assert all(button.callback_data.startswith("horo_settings:now:tomorrow:") for button in buttons)
    assert context.user_data == {}
    update.callback_query.data = "horo_settings:now:tomorrow:taurus"
    await horoscope.horoscope_settings_callback(update, context)
    deliver.assert_awaited_once_with(context.bot, 42, "taurus", "tomorrow")
    assert "horo_sign" not in context.user_data


@pytest.mark.asyncio
async def test_duplicate_clicks_are_coalesced_and_failure_can_be_retried(monkeypatch):
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value={"sign": "leo"}))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        entered.set()
        await release.wait()
        return False

    deliver = AsyncMock(side_effect=slow)
    monkeypatch.setattr("app.handlers.scheduled_horoscopes._deliver_horoscope", deliver)
    update, context = request()
    first = asyncio.create_task(horoscope.horoscope_settings_callback(update, context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await horoscope.horoscope_settings_callback(update, context)
        assert deliver.await_count == 1
    finally:
        release.set()
        await first
    await horoscope.horoscope_settings_callback(update, context)
    assert deliver.await_count == 2
    assert not context.user_data.get("horo_now_busy")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", ["horo_settings:now:invalid", "horo_settings:now:today:bad-sign", "horo_settings:now:today:leo:extra"]
)
async def test_invalid_manual_callback_never_generates(monkeypatch, data):
    deliver = AsyncMock()
    monkeypatch.setattr("app.handlers.scheduled_horoscopes._deliver_horoscope", deliver)
    update, context = request(data)
    await horoscope.horoscope_settings_callback(update, context)
    deliver.assert_not_awaited()
    assert update.callback_query.answer.await_count == 1
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_success_has_short_cooldown(monkeypatch):
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value={"sign": "leo"}))
    now = [100.0]
    monkeypatch.setattr(horoscope.time, "monotonic", lambda: now[0])
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr("app.handlers.scheduled_horoscopes._deliver_horoscope", deliver)
    update, context = request()
    await horoscope.horoscope_settings_callback(update, context)
    await horoscope.horoscope_settings_callback(update, context)
    assert deliver.await_count == 1
    now[0] += 11
    await horoscope.horoscope_settings_callback(update, context)
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_generation_allows_retry(monkeypatch):
    monkeypatch.setattr(horoscope, "get_horoscope_subscription", AsyncMock(return_value={"sign": "leo"}))
    deliver = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("app.handlers.scheduled_horoscopes._deliver_horoscope", deliver)
    update, context = request()
    with pytest.raises(asyncio.CancelledError):
        await horoscope.horoscope_settings_callback(update, context)
    assert not context.user_data.get("horo_now_busy")
    assert "horo_now_last_success" not in context.user_data


@pytest.mark.asyncio
async def test_long_horoscope_is_delivered_completely_in_safe_chunks(monkeypatch):
    from app.handlers.scheduled_horoscopes import _deliver_horoscope

    body = "**" + ("Прогноз на день. " * 400) + "Конец прогноза.**"
    monkeypatch.setattr("app.intent_router._handle_horoscope", AsyncMock(return_value=SimpleNamespace(text=body)))
    bot = SimpleNamespace(send_message=AsyncMock())
    assert await _deliver_horoscope(bot, 42, "leo", "today")
    texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert len(texts) > 1
    assert all(len(text) <= 4096 for text in texts)
    assert all(text.count("<b>") == text.count("</b>") for text in texts)
    assert "Конец прогноза." in texts[-1]
