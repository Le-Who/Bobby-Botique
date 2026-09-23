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
async def test_slow_horoscope_does_not_block_another_due_recipient() -> None:
    second_started = asyncio.Event()
    release = asyncio.Event()

    async def due(_hour, _minute, kind):
        if kind == "today":
            return [{"user_id": 7, "sign": "aries"}, {"user_id": 8, "sign": "taurus"}]
        return []

    async def deliver(_bot, user_id, _sign, _kind):
        if user_id == 7:
            await release.wait()
        else:
            second_started.set()
        return True

    with (
        patch("app.repos.settings_repo.get_global_setting", new=AsyncMock(return_value="on")),
        patch("app.handlers.scheduled_horoscopes.get_due_horoscope_subscriptions", new=due),
        patch("app.handlers.scheduled_horoscopes._deliver_horoscope", new=deliver),
        patch("app.handlers.scheduled_horoscopes.mark_horoscope_sent", new=AsyncMock()),
    ):
        task = asyncio.create_task(scheduled_horoscopes.check_and_send_horoscopes(SimpleNamespace(bot=object())))
        try:
            await asyncio.wait_for(second_started.wait(), 0.2)
        finally:
            release.set()
            await task
