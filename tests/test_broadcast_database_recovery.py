"""Retry setup on a fresh lease, never replay an entered broadcast transaction."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from asyncpg.pool import PoolConnectionProxy
from asyncpg.transaction import Transaction

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


@pytest.fixture
def native_pool(monkeypatch):
    def install(*, stage="context", detach=True, cleanup_entered=None):
        connections = []

        class NativeConnection:
            def __init__(self):
                self.ordinal = len(connections) + 1
                self.closed = False
                self.proxy = None
                self._pool_release_ctr = 0
                self._top_xact = None
                self._protocol = SimpleNamespace(is_in_transaction=lambda: False)

            def _set_proxy(self, proxy):
                self.proxy = proxy

            def is_closed(self):
                return self.closed

            def drop_transport(self):
                self.closed = True
                if detach and self.proxy is not None:
                    self.proxy._detach()

            def transaction(self):
                transaction = Transaction(self, None, False, False)
                if self.ordinal == 1 and stage == "before_begin":
                    self.drop_transport()
                if stage in {"release_misuse", "released_closed_misuse"}:
                    self._pool_release_ctr += 1
                if stage == "released_closed_misuse":
                    self.drop_transport()
                return transaction

            async def execute(self, query):
                if stage == "open_interface":
                    raise asyncpg.InterfaceError("synthetic interface misuse on a live connection")
                if stage == "cancel_rollback" and query == "ROLLBACK;":
                    cleanup_entered.set()
                    await asyncio.Event().wait()
                return "OK"

        @asynccontextmanager
        async def acquire(**kwargs):
            raw = NativeConnection()
            connections.append(raw)
            try:
                yield PoolConnectionProxy(None, raw)
            finally:
                if stage == "cancel_release" and raw.ordinal == 1:
                    cleanup_entered.set()
                    await asyncio.Event().wait()

        async def context(*args, **kwargs):
            raw = connections[-1]
            if raw.ordinal == 1 and stage == "context":
                raw.drop_transport()
                raise asyncpg.ConnectionDoesNotExistError("synthetic context connection loss")
            if raw.ordinal == 1 and stage in {"cancel_rollback", "cancel_release"}:
                raise TimeoutError("synthetic setup timeout before owner cancellation")

        async def clear_context(**kwargs):
            if stage == "cancel_clear":
                connections[-1].drop_transport()
                cleanup_entered.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(journal.db.db_manager, "pool", SimpleNamespace(acquire=acquire))
        monkeypatch.setattr(journal.db, "set_user_context", AsyncMock(side_effect=context))
        monkeypatch.setattr(journal.db, "clear_user_context", AsyncMock(side_effect=clear_context))
        monkeypatch.setattr(asyncio, "sleep", AsyncMock())
        return connections

    return install


@pytest.mark.parametrize("stage", ["before_begin", "context"])
@pytest.mark.parametrize("detach", [False, True])
async def test_native_closed_connection_guard_during_setup_retries(native_pool, stage, detach):
    connections = native_pool(stage=stage, detach=detach)
    async with journal.transaction():
        assert connections[-1].ordinal == 2
    assert len(connections) == 2


@pytest.mark.parametrize("detach", [False, True])
async def test_native_rollback_cannot_mask_original_body_failure(native_pool, detach):
    connections = native_pool(stage="body", detach=detach)
    original = asyncpg.ConnectionDoesNotExistError("synthetic body connection loss")
    with pytest.raises(asyncpg.ConnectionDoesNotExistError) as caught:
        async with journal.transaction():
            connections[-1].drop_transport()
            raise original
    assert caught.value is original
    assert len(connections) == 1


@pytest.mark.parametrize("detach", [False, True])
async def test_native_rollback_cannot_turn_owner_cancellation_into_failure(native_pool, detach):
    connections = native_pool(stage="body", detach=detach)
    entered = asyncio.Event()

    async def operation():
        async with journal.transaction():
            connections[-1].drop_transport()
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel("owner stop")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert len(connections) == 1


@pytest.mark.parametrize("stage", ["open_interface", "release_misuse", "released_closed_misuse"])
async def test_native_interface_misuse_stays_fatal(native_pool, stage):
    connections = native_pool(stage=stage)
    with pytest.raises(asyncpg.InterfaceError):
        async with journal.transaction():
            pytest.fail("interface misuse must not enter the body")
    assert len(connections) == 1


@pytest.mark.parametrize("stage", ["cancel_rollback", "cancel_release", "cancel_clear"])
async def test_new_owner_cancellation_during_cleanup_wins_over_earlier_failure(native_pool, stage):
    entered = asyncio.Event()
    connections = native_pool(stage=stage, cleanup_entered=entered)
    bodies = []

    async def operation():
        async with journal.transaction():
            bodies.append(connections[-1].ordinal)

    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel("owner stop during cleanup")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert len(connections) == 1
    assert bodies == ([1] if stage == "cancel_clear" else [])
