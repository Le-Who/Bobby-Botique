"""Scheduled Tarot uses prepared content and the existing journal owner."""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from app import tarot_daily
from app.broadcast_delivery import DeliveryOwner
from app.broadcast_jobs import BroadcastService
from app.handlers import tarot_daily as handler
from app.tarot import iter_daily_card_variants


def prepared_reading(body="Совет на сегодня: сохраняйте спокойствие."):
    return tarot_daily.TarotDailyReading(
        reading_date=date(2026, 11, 9),
        card_name="The Fool",
        orientation="Прямая",
        language="ru",
        body_markdown=body,
        model_name="synthetic-model",
    )


@pytest.mark.asyncio
async def test_tarot_broadcast_sends_prepared_reading_and_unsubscribe_control(monkeypatch):
    monkeypatch.setattr(tarot_daily, "get_prepared_daily_reading", AsyncMock(return_value=prepared_reading()))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=81)))
    batch = {"channel": "tarot", "mode": "delivery", "effective_date": date(2026, 11, 9)}

    await BroadcastService()._send(batch, 101, bot)

    payload = bot.send_message.await_args.kwargs
    assert payload["chat_id"] == 101
    assert "Совет на сегодня" in payload["text"]
    assert "Подписаться" not in payload["text"]
    assert payload["reply_markup"].inline_keyboard[0][0].callback_data == "tarot_daily:unsubscribe"


@pytest.mark.asyncio
async def test_missing_tarot_reading_never_sends_an_invite_or_error_instead(monkeypatch):
    monkeypatch.setattr(tarot_daily, "get_prepared_daily_reading", AsyncMock(return_value=None))
    bot = SimpleNamespace(send_message=AsyncMock())
    batch = {"channel": "tarot", "mode": "delivery", "effective_date": date(2026, 11, 9)}
    with pytest.raises(ValueError, match="tarot_reading_unavailable"):
        await BroadcastService()._send(batch, 101, bot)
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_waits_for_prepared_card_then_only_enqueues_delivery(monkeypatch):
    now = datetime(2026, 11, 9, 8, 5, tzinfo=UTC)
    monkeypatch.setattr(handler, "datetime", SimpleNamespace(now=lambda **_kwargs: now))
    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", AsyncMock(return_value="on"))
    monkeypatch.setattr(
        "app.repos.tarot_daily_subscriptions.get_due_for_current_hour",
        AsyncMock(return_value=[{"user_id": 101, "slot_date": date(2026, 11, 9)}]),
    )
    variants = [{"name": "The Fool", "orientation": "Прямая"}]
    monkeypatch.setattr("app.tarot.iter_daily_card_variants", lambda: variants)
    ready = AsyncMock(side_effect=[[], [prepared_reading()]])
    monkeypatch.setattr(tarot_daily, "get_prepared_daily_readings", ready, raising=False)
    enqueue = AsyncMock()
    monkeypatch.setattr("app.broadcast_jobs.enqueue_scheduled", enqueue)
    bot = SimpleNamespace(send_message=AsyncMock())

    await handler.check_and_send_tarot_readings(SimpleNamespace(bot=bot))
    enqueue.assert_not_awaited()
    await handler.check_and_send_tarot_readings(SimpleNamespace(bot=bot))
    enqueue.assert_awaited_once_with(
        channel="tarot", user_ids=[101], effective_date=tarot_daily.today_reading_date(now), mode="delivery"
    )
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["off", ConnectionError("synthetic storage outage")])
async def test_scheduler_fails_closed_when_delivery_is_paused_or_switch_unavailable(monkeypatch, switch):
    monkeypatch.setattr(
        "app.repos.settings_repo.get_global_setting",
        AsyncMock(side_effect=switch) if isinstance(switch, Exception) else AsyncMock(return_value=switch),
    )
    due = AsyncMock()
    monkeypatch.setattr("app.repos.tarot_daily_subscriptions.get_due_for_current_hour", due)
    enqueue = AsyncMock()
    monkeypatch.setattr("app.broadcast_jobs.enqueue_scheduled", enqueue)

    await handler.check_and_send_tarot_readings(SimpleNamespace(bot=object()))

    due.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outage", [False, True])
async def test_missing_tarot_switch_stays_disabled_through_real_settings_reader(monkeypatch, outage):
    from app.repos import settings_repo

    monkeypatch.setattr(settings_repo, "_cache", {})
    monkeypatch.setattr(
        settings_repo.db,
        "db_query",
        AsyncMock(side_effect=ConnectionError("synthetic outage")) if outage else AsyncMock(return_value=[]),
    )
    due = AsyncMock(return_value=[])
    monkeypatch.setattr("app.repos.tarot_daily_subscriptions.get_due_for_current_hour", due)

    await handler.check_and_send_tarot_readings(SimpleNamespace(bot=object()))

    due.assert_not_awaited()


@pytest.mark.asyncio
async def test_tarot_timeout_is_journaled_unknown_without_fallback_or_second_send(monkeypatch):
    monkeypatch.setattr(tarot_daily, "get_prepared_daily_reading", AsyncMock(return_value=prepared_reading()))
    journal = SimpleNamespace(
        start_attempt=AsyncMock(return_value={"attempt_id": "synthetic-attempt"}),
        finish_attempt=AsyncMock(return_value=True),
    )
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
    owner = DeliveryOwner(journal, batch_id="batch", recipient_id=101, token="owner", bot=bot, checkpoint=AsyncMock())
    batch = {"channel": "tarot", "mode": "delivery", "effective_date": date(2026, 11, 9)}

    result = await owner.deliver(lambda proxy: BroadcastService()._send(batch, 101, proxy))

    assert result.outcome == "unknown_delivery"
    bot.send_message.assert_awaited_once()
    assert journal.finish_attempt.await_args.args[2].outcome == "unknown_delivery"


@pytest.mark.asyncio
async def test_tarot_sender_escapes_card_name_and_splits_long_prepared_text(monkeypatch):
    monkeypatch.setattr(handler, "daily_card_for_user", lambda *_args: ("<synthetic card>", "Прямая"))
    reading = prepared_reading("Спокойный совет. " * 600)
    monkeypatch.setattr(tarot_daily, "get_prepared_daily_reading", AsyncMock(return_value=reading))
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=82)))

    await handler.deliver_tarot_reading(bot, 101, date(2026, 11, 9), slot_date=date(2026, 11, 10))

    payloads = [call.kwargs for call in bot.send_message.await_args_list]
    assert len(payloads) > 1
    assert all(len(payload["text"]) <= 3900 for payload in payloads)
    assert "&lt;synthetic card&gt;" in payloads[0]["text"]
    assert all(payload["reply_markup"] is None for payload in payloads[:-1])
    assert payloads[-1]["reply_markup"].inline_keyboard[0][0].callback_data == "tarot_daily:unsubscribe"


def test_tarot_card_identity_is_stable_for_user_and_local_day():
    day = date(2026, 11, 9)
    selections = {handler.daily_card_for_user(101, day) for _ in range(10)}
    assert len(selections) == 1
    assert selections <= {(card["name"], card["orientation"]) for card in iter_daily_card_variants()}
