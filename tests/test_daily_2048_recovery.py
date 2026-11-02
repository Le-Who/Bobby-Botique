"""Transient preparation failures must not suppress a ready daily challenge."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.games import daily_2048 as game
from app.handlers import daily_2048 as handler
from app.repos import daily_2048 as repo

DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


@pytest.fixture
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(game.asyncio, "sleep", AsyncMock())


class LostCommitResponseTransport:
    """Keep a committed SQL insert, then lose its transaction acknowledgment."""

    def __init__(self, failure):
        self.failure = failure
        self.row = None
        self.writes = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure

    async def fetchrow(self, query, *args):
        assert "INSERT INTO public.daily_2048_puzzles" in query
        assert self.row is None, "recovery must read the committed challenge before another insert"
        self.row = asdict(repo.Daily2048Puzzle(*args))
        self.writes += 1
        return copy.deepcopy(self.row)

    async def query(self, query, params):
        if "WHERE puzzle_date = $1" in query:
            return [copy.deepcopy(self.row)] if self.row is not None else []
        assert "FROM public.daily_2048_puzzles" in query
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError(), asyncpg.PostgresConnectionError()])
async def test_prepare_recovers_a_lost_commit_response_without_replacing_the_challenge(
    monkeypatch, no_retry_wait, failure
):
    transport = LostCommitResponseTransport(failure)
    monkeypatch.setattr(repo.db.db_manager, "pool", transport)
    monkeypatch.setattr(repo.db, "db_query", transport.query)

    puzzles = await game.ensure_prepared_puzzles(now=NOW, days_ahead=0)

    assert len(puzzles) == 1
    assert puzzles[0].puzzle_date == DAY
    assert puzzles[0].board == transport.row["board"]
    assert puzzles[0].seed == transport.row["seed"]
    assert puzzles[0].revision == 1
    assert transport.writes == 1


@pytest.mark.asyncio
async def test_prepare_retries_each_date_and_keeps_successful_dates_in_order(monkeypatch, no_retry_wait):
    attempts = {}

    async def ensure(day):
        attempts[day] = attempts.get(day, 0) + 1
        if attempts[day] == 1:
            raise TimeoutError
        return repo.build_default_puzzle(day)

    monkeypatch.setattr(repo, "ensure_puzzle", ensure)

    puzzles = await game.ensure_prepared_puzzles(now=NOW, days_ahead=2)

    assert [puzzle.puzzle_date for puzzle in puzzles] == [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    assert list(attempts.values()) == [2, 2, 2]


def capture_scheduled_deliveries(monkeypatch):
    from app import broadcast_jobs
    from app.handlers import daily_crocodile

    scheduled = []

    async def enqueue(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(repo, "today_puzzle_date", lambda now=None: DAY)
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=repo.build_default_puzzle(DAY)))
    monkeypatch.setattr(daily_crocodile, "is_daily_delivery_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(handler.daily_delivery_repo, "get_due_deliveries", AsyncMock(return_value=[{"user_id": 777}]))
    monkeypatch.setattr(handler.daily_delivery_repo, "get_discovery_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(broadcast_jobs, "enqueue_scheduled", enqueue)
    return scheduled


@pytest.mark.asyncio
async def test_future_timeout_does_not_block_the_ready_today_delivery(monkeypatch, no_retry_wait):
    scheduled = capture_scheduled_deliveries(monkeypatch)
    attempts = {}

    async def ensure(day):
        attempts[day] = attempts.get(day, 0) + 1
        if day != DAY:
            raise TimeoutError
        return repo.build_default_puzzle(day)

    monkeypatch.setattr(repo, "ensure_puzzle", ensure)

    await handler.check_daily_2048_jobs(SimpleNamespace(), admin_mode="crocodile")

    assert scheduled == [
        {
            "channel": "daily_challenge",
            "user_ids": [777],
            "effective_date": DAY,
            "variant": "2048",
            "mode": "delivery",
        }
    ]
    assert attempts == {DAY: 1, DAY + timedelta(days=1): 2}


@pytest.mark.asyncio
async def test_today_failure_stops_delivery_after_the_bounded_retry(monkeypatch, no_retry_wait):
    scheduled = capture_scheduled_deliveries(monkeypatch)
    attempts = {}

    async def ensure(day):
        attempts[day] = attempts.get(day, 0) + 1
        if day == DAY:
            raise TimeoutError
        return repo.build_default_puzzle(day)

    monkeypatch.setattr(repo, "ensure_puzzle", ensure)

    await handler.check_daily_2048_jobs(SimpleNamespace(), admin_mode="crocodile")

    assert scheduled == []
    assert attempts[DAY] == 2


@pytest.mark.asyncio
async def test_nontransient_future_failure_is_not_retried_or_hidden(monkeypatch, no_retry_wait):
    attempts = []

    async def ensure(day):
        attempts.append(day)
        if day != DAY:
            raise ValueError("invalid puzzle")
        return repo.build_default_puzzle(day)

    monkeypatch.setattr(repo, "ensure_puzzle", ensure)

    with pytest.raises(ValueError, match="invalid puzzle"):
        await game.ensure_prepared_puzzles(now=NOW, days_ahead=1)

    assert attempts == [DAY, DAY + timedelta(days=1)]


@pytest.mark.asyncio
async def test_today_failure_cancels_and_drains_other_preparation_tasks(monkeypatch):
    future_started = asyncio.Event()
    future_finished = asyncio.Event()
    peers = []

    async def ensure(day):
        if day == DAY:
            await future_started.wait()
            raise ValueError("invalid today")
        peers.append(asyncio.current_task())
        future_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            future_finished.set()

    monkeypatch.setattr(repo, "ensure_puzzle", ensure)

    try:
        with pytest.raises(ValueError, match="invalid today"):
            await game.ensure_prepared_puzzles(now=NOW, days_ahead=1)
        assert future_finished.is_set(), "request-scoped preparation must finish before the owner exits"
        assert all(peer.done() for peer in peers)
    finally:
        for peer in peers:
            peer.cancel()
        await asyncio.gather(*peers, return_exceptions=True)


@pytest.mark.asyncio
async def test_caller_cancellation_drains_all_dates_without_retrying(monkeypatch):
    all_started = asyncio.Event()
    started = []
    finished = []

    async def ensure(day):
        started.append(day)
        if len(started) == 3:
            all_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(day)

    monkeypatch.setattr(repo, "ensure_puzzle", ensure)
    preparation = asyncio.create_task(game.ensure_prepared_puzzles(now=NOW, days_ahead=2))
    try:
        await all_started.wait()
        preparation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparation
        assert sorted(finished) == [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
        assert len(started) == 3
    finally:
        preparation.cancel()
        await asyncio.gather(preparation, return_exceptions=True)
