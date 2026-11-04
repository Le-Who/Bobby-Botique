"""Retry setup on a fresh lease, never replay an entered broadcast transaction."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest

from app import broadcast_jobs as jobs
from app.repos import broadcast_jobs as journal

pytestmark = pytest.mark.asyncio


class PoolProbe:
    def __init__(self, *, stage="begin", failures=1, error_type=asyncpg.ConnectionDoesNotExistError):
        self.stage = stage
        self.failures = failures
        self.error_type = error_type
        self.acquired = []
        self.released = []
        self.bodies = []

    def fail(self, stage, ordinal):
        if stage == self.stage and ordinal <= self.failures:
            raise self.error_type("synthetic disconnected broadcast connection")

    @asynccontextmanager
    async def acquire(self, **kwargs):
        ordinal = len(self.acquired) + 1
        self.acquired.append(ordinal)
        self.fail("acquire", ordinal)

        @asynccontextmanager
        async def transaction():
            self.fail("begin", ordinal)
            yield
            self.fail("commit", ordinal)

        connection = SimpleNamespace(transaction=transaction, ordinal=ordinal)
        try:
            yield connection
        finally:
            self.released.append(ordinal)


@pytest.fixture
def pool_probe(monkeypatch):
    def install(**kwargs):
        pool = PoolProbe(**kwargs)
        monkeypatch.setattr(journal.db.db_manager, "pool", pool)

        async def context(*args, conn, **kwargs):
            pool.fail("context", conn.ordinal)

        monkeypatch.setattr(journal.db, "set_user_context", AsyncMock(side_effect=context))
        monkeypatch.setattr(journal.db, "clear_user_context", AsyncMock())
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        return pool

    return install


@pytest.mark.parametrize("stage", ["acquire", "begin", "context"])
@pytest.mark.parametrize(
    "error_type",
    [
        asyncpg.ConnectionDoesNotExistError,
        asyncpg.AdminShutdownError,
        asyncpg.CrashShutdownError,
        asyncpg.CannotConnectNowError,
        ConnectionResetError,
        TimeoutError,
    ],
)
async def test_setup_disconnect_reacquires_before_entering_body(pool_probe, stage, error_type):
    pool = pool_probe(stage=stage, error_type=error_type)
    async with journal.transaction() as connection:
        pool.bodies.append(connection.ordinal)
    assert pool.acquired == [1, 2]
    assert pool.bodies == [2]
    assert pool.released == ([2] if stage == "acquire" else [1, 2])
    journal.db.clear_user_context.assert_awaited_once_with(conn=connection)


async def test_exhausted_setup_is_bounded_and_does_not_enter_body(pool_probe):
    pool = pool_probe(failures=100)
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        async with journal.transaction():
            pytest.fail("unavailable database must not enter the transaction body")
    assert pool.acquired == [1, 2, 3]
    assert pool.released == [1, 2, 3]


async def test_query_failure_does_not_replay_broadcast_effects(pool_probe):
    pool = pool_probe(failures=0)
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        async with journal.transaction() as connection:
            pool.bodies.append(connection.ordinal)
            raise asyncpg.ConnectionDoesNotExistError("synthetic disconnect after business work started")
    assert pool.acquired == pool.released == pool.bodies == [1]


async def test_uncertain_commit_does_not_replay_broadcast_effects(pool_probe):
    pool = pool_probe(stage="commit")
    with pytest.raises(asyncpg.ConnectionDoesNotExistError):
        async with journal.transaction() as connection:
            pool.bodies.append(connection.ordinal)
    assert pool.acquired == pool.released == pool.bodies == [1]


async def test_non_connection_setup_error_is_not_retried(pool_probe):
    pool = pool_probe(error_type=asyncpg.UndefinedTableError)
    with pytest.raises(asyncpg.UndefinedTableError):
        async with journal.transaction():
            pytest.fail("schema errors must stay visible")
    assert pool.acquired == pool.released == [1]


async def test_cancellation_during_retry_does_not_acquire_again(pool_probe, monkeypatch):
    pool = pool_probe()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        async with journal.transaction():
            pytest.fail("cancelled setup must not enter the body")
    assert pool.acquired == pool.released == [1]


async def test_external_connection_keeps_its_callers_ownership(pool_probe):
    pool = pool_probe()
    external = object()
    async with journal.transaction(external) as connection:
        assert connection is external
    assert pool.acquired == []
    journal.db.set_user_context.assert_not_awaited()
    journal.db.clear_user_context.assert_not_awaited()


@pytest.mark.parametrize(
    "error_type",
    [
        asyncpg.ConnectionDoesNotExistError,
        asyncpg.AdminShutdownError,
        asyncpg.CrashShutdownError,
        asyncpg.CannotConnectNowError,
        ConnectionResetError,
        TimeoutError,
    ],
)
async def test_periodic_recovery_defers_outage_and_succeeds_on_next_tick(monkeypatch, error_type):
    recover = AsyncMock(side_effect=[error_type("synthetic maintenance outage"), {"recovered": 2}])
    monkeypatch.setattr(jobs, "service", SimpleNamespace(recover=recover))
    record = Mock(return_value="synthetic-maintenance-error")
    monkeypatch.setattr("app.observability.events.record_exception", record)
    result = await jobs.recover_broadcast_jobs()
    assert result == {"deferred": True, "reason_code": "database_unavailable"}
    assert await jobs.recover_broadcast_jobs() == {"recovered": 2}
    record.assert_called_once()
    assert record.call_args.args[0] == "broadcast.recovery_deferred"
    assert record.call_args.kwargs["level"] == "warning"


@pytest.mark.parametrize("error_type", [asyncpg.UndefinedTableError, asyncio.CancelledError])
async def test_periodic_recovery_does_not_hide_schema_errors_or_cancellation(monkeypatch, error_type):
    recover = AsyncMock(side_effect=error_type)
    monkeypatch.setattr(jobs, "service", SimpleNamespace(recover=recover))
    with pytest.raises(error_type):
        await jobs.recover_broadcast_jobs()
