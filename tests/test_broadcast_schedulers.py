"""Schedulers enqueue bounded journal jobs rather than owning Telegram sends."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_2048_scheduler_only_enqueues_delivery_and_discovery():
    from app.handlers.daily_2048 import check_daily_2048_jobs

    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    with (
        patch("app.games.daily_2048.ensure_prepared_puzzles", new=AsyncMock()),
        patch("app.handlers.daily_2048.repo.get_puzzle", new=AsyncMock(return_value=SimpleNamespace(status="ready"))),
        patch("app.handlers.daily_crocodile.is_daily_delivery_enabled", new=AsyncMock(return_value=True)),
        patch(
            "app.handlers.daily_2048.daily_delivery_repo.get_due_deliveries",
            new=AsyncMock(return_value=[{"user_id": 101}]),
        ),
        patch(
            "app.handlers.daily_2048.daily_delivery_repo.get_discovery_candidates",
            new=AsyncMock(return_value=[{"user_id": 102}]),
        ),
        patch("app.broadcast_jobs.enqueue_scheduled", new=AsyncMock()) as enqueue,
    ):
        await check_daily_2048_jobs(SimpleNamespace(bot=bot), admin_mode="2048")
    assert enqueue.await_count == 2
    assert {call.kwargs["mode"] for call in enqueue.await_args_list} == {"offer", "delivery"}
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_horoscope_scheduler_enqueues_without_network_inside_slot_transaction():
    from app.handlers.scheduled_horoscopes import check_and_send_horoscopes

    with (
        patch("app.repos.settings_repo.get_global_setting", new=AsyncMock(return_value="on")),
        patch(
            "app.handlers.scheduled_horoscopes.get_due_horoscope_subscriptions",
            new=AsyncMock(return_value=[{"user_id": 101, "sign": "aries"}]),
        ),
        patch("app.handlers.scheduled_horoscopes._deliver_horoscope", new=AsyncMock()) as send,
        patch("app.broadcast_jobs.enqueue_scheduled", new=AsyncMock()) as enqueue,
    ):
        await check_and_send_horoscopes(SimpleNamespace(bot=object()))
    assert enqueue.await_count == 2
    assert {call.kwargs["variant"] for call in enqueue.await_args_list} == {"today", "tomorrow"}
    send.assert_not_awaited()
