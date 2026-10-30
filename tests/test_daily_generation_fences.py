"""A superseded generation cannot publish or release the new owner."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app import tarot_daily
from app.repos import tarot_generation as repo

DAY = date(2026, 10, 28)


class GenerationDatabase:
    def __init__(self):
        self.row = {"revision": 0, "token": None}
        self.readings = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql, *args):
        if "INSERT INTO" in sql:
            self.row = {"revision": self.row["revision"] + 1, "token": args[2]}
        elif "UPDATE public" in sql:
            if (self.row["revision"], self.row["token"]) != (args[2], args[3]):
                return None
            self.row["token"] = None
        return dict(self.row)

    async def execute(self, sql, *args):
        if "INSERT INTO public.tarot_daily_readings" in sql:
            self.readings.append(args[4])


@pytest.mark.asyncio
async def test_superseded_owner_cannot_write_or_release_new_generation():
    connection = GenerationDatabase()
    old = await repo.begin_generation(DAY, "ru", conn=connection)
    new = await repo.begin_generation(DAY, "ru", conn=connection)
    values = {
        "reading_date": DAY,
        "card_name": "Маг",
        "orientation": "Прямая",
        "language": "ru",
        "body_markdown": "synthetic",
        "conn": connection,
    }
    with pytest.raises(repo.TarotGenerationConflict):
        await tarot_daily.upsert_prepared_daily_reading(**values, ticket=old)
    assert not await repo.finish_generation(old, conn=connection)
    assert connection.row["token"] == new.token
    await tarot_daily.upsert_prepared_daily_reading(**values, ticket=new)
    assert connection.readings == ["synthetic"]
    assert await repo.finish_generation(new, conn=connection)


@pytest.mark.asyncio
async def test_inline_cache_fill_cannot_replace_active_generation():
    connection = GenerationDatabase()
    await repo.begin_generation(DAY, "ru", conn=connection)
    await tarot_daily.upsert_prepared_daily_reading(
        reading_date=DAY,
        card_name="Маг",
        orientation="Прямая",
        language="ru",
        body_markdown="late inline",
        conn=connection,
    )
    assert not connection.readings


@pytest.mark.asyncio
async def test_provider_swallowing_cancel_cannot_publish_late_result(monkeypatch):
    from app.games.daily_execution import checkpoint

    entered = asyncio.Event()
    writer = AsyncMock()

    async def owner():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # adversarial provider adapter
        await checkpoint()
        await writer()

    task = asyncio.create_task(owner())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_trivia_client_revision_is_checked_before_generation(monkeypatch):
    from types import SimpleNamespace

    from app.games import daily_trivia

    monkeypatch.setattr(daily_trivia.repo, "get_puzzle", AsyncMock(return_value=SimpleNamespace(revision=7)))
    provider = AsyncMock()
    monkeypatch.setattr(daily_trivia, "generate_question_lane", provider)
    with pytest.raises(daily_trivia.repo.RevisionConflictError):
        await daily_trivia.prepare_daily_puzzle(DAY, force=True, expected_revision=6)
    provider.assert_not_awaited()
