from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers import scheduled_horoscopes
from app.repos import horoscope_subscriptions


@pytest.mark.asyncio
async def test_due_horoscopes_include_same_local_day_after_exact_minute(monkeypatch) -> None:
    captured = {}

    async def fake_query(query, params=(), **kwargs):
        captured["sql"] = query
        captured["params"] = params
        return []

    monkeypatch.setattr(horoscope_subscriptions, "db_query", fake_query)
    await horoscope_subscriptions.get_due_horoscope_subscriptions(6, 5, "today")

    sql = captured["sql"]
    assert "<= (MOD(($1::int + utc_offset + 48), 24) * 60 + $2::int)" in sql
    assert "make_interval(hours => utc_offset)" in sql
    assert captured["params"] == (6, 5)


@pytest.mark.asyncio
async def test_scheduler_hands_due_recipients_to_workers_without_waiting_for_delivery() -> None:
    async def due(_hour, _minute, kind):
        if kind == "today":
            return [{"user_id": 7, "sign": "aries"}, {"user_id": 8, "sign": "taurus"}]
        return []

    enqueue = AsyncMock()
    deliver = AsyncMock(side_effect=AssertionError("Scheduler must not own network delivery"))
    with (
        patch("app.repos.settings_repo.get_global_setting", new=AsyncMock(return_value="on")),
        patch("app.handlers.scheduled_horoscopes.get_due_horoscope_subscriptions", new=due),
        patch("app.handlers.scheduled_horoscopes._deliver_horoscope", new=deliver),
        patch("app.broadcast_jobs.enqueue_scheduled", new=enqueue),
    ):
        await asyncio.wait_for(scheduled_horoscopes.check_and_send_horoscopes(SimpleNamespace(bot=object())), 2)
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["user_ids"] == [7, 8]
    assert enqueue.await_args.kwargs["variant"] == "today"
    deliver.assert_not_awaited()
