"""Account erasure must retire process-local state writes, including SQL retries."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app import database, state
from app.repos import users

pytestmark = pytest.mark.asyncio


class StateErasureDriver:
    _closed = False

    def __init__(self):
        self.failed_write = asyncio.Event()
        self.release_retry = asyncio.Event()
        self.epoch = 100
        self.write_attempts = 0
        self.commits = []
        self.delete_error = None
        self.retry_first = True
        self.pause_commit = False
        self.commit_started = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.commit_error = None

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield
        if self.pause_commit:
            self.commit_started.set()
            await self.release_commit.wait()
        if self.commit_error is not None:
            raise self.commit_error

    async def execute(self, sql, *params):
        pass

    async def fetch(self, sql, *params):
        if "INSERT INTO user_state" in sql:
            self.write_attempts += 1
            if self.retry_first and self.write_attempts == 1:
                self.failed_write.set()
                raise asyncpg.InterfaceError("synthetic connection reset before commit")
            if self.retry_first:
                await self.release_retry.wait()
            self.commits.append((self.epoch, params[2]))
            return []
        if "private_data_blocked IS TRUE" in sql:
            return [{"user_id": 42}]
        if "DELETE FROM public.users" in sql:
            if self.delete_error is not None:
                raise self.delete_error
            self.epoch = None
        return []


@pytest.fixture
def erasure(monkeypatch):
    from app.queue import TaskQueue
    from app.queue_store import MemoryQueueStore

    monkeypatch.setattr("app.queue.task_queue", TaskQueue(store=MemoryQueueStore()))
    driver = StateErasureDriver()
    monkeypatch.setattr(database.db_manager, "pool", driver)
    monkeypatch.setattr(database.db_manager, "reconnect", AsyncMock())
    monkeypatch.setattr(database.db_manager, "_user_auth_cache", {42: True})
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=2))
    monkeypatch.setattr(state, "_pending_persists", {})
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr(users, "is_admin", lambda uid: False)
    monkeypatch.setattr("app.context.summarizer.cancel_user_summarization_tasks", AsyncMock())
    monkeypatch.setattr("app.repos.memory_autosave.cancel_user_memory_tasks", AsyncMock())
    monkeypatch.setattr(
        "app.voice_engine.get_voice_reply_manager", lambda: SimpleNamespace(purge_user_jobs=AsyncMock())
    )
    monkeypatch.setattr("app.middleware.dedup.clear_user_dedup", lambda uid: None)
    monkeypatch.setattr("app.group_chat.group_chat_manager.apply_account_erasure", AsyncMock())
    monkeypatch.setattr(users, "set_user_context", AsyncMock())
    monkeypatch.setattr(users, "clear_user_context", AsyncMock())

    @asynccontextmanager
    async def privacy_barrier(*args, **kwargs):
        yield 12, True

    monkeypatch.setattr("app.repos.memory_consent.private_data_barrier", privacy_barrier)
    yield driver
    for timer in state._pending_persists.values():
        timer.cancel()


async def test_erasure_waits_for_real_sql_retry_and_invalidates_queued_writer(erasure):
    current = state.get_user_state(42)
    current._loaded_from_db = True
    current.selected_document_id = 5
    first = asyncio.create_task(state._persist(current))
    await erasure.failed_write.wait()
    deletion = asyncio.create_task(users.erase_user_account(42))
    queued = None
    try:
        await asyncio.sleep(0)
        assert not deletion.done(), "account erasure passed an in-flight state retry"
        current.selected_document_id = 6
        queued = asyncio.create_task(state._persist(current))
        erasure.release_retry.set()
        await asyncio.wait_for(asyncio.gather(first, deletion), 3)
        erasure.epoch = 200  # The same Telegram ID can be authorized again.
        await queued
        assert erasure.commits == [(100, 5)]
        assert current._user_id == 0
        assert state.get_user_state(42) is not current
    finally:
        erasure.release_retry.set()
        tasks = [first, deletion, *([queued] if queued is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_erasure_lock_wait_preserves_shared_writer_and_account(erasure, monkeypatch, outcome):
    monkeypatch.setattr(users, "_STATE_ERASURE_WAIT_SECONDS", 0.01, raising=False)
    current = state.get_user_state(42)
    current.selected_document_id = 5
    writer = asyncio.create_task(state._persist(current))
    await erasure.failed_write.wait()
    deletion = asyncio.create_task(users.erase_user_account(42))
    try:
        if outcome == "cancel":
            await asyncio.sleep(0)
            deletion.cancel()
        done, _ = await asyncio.wait({deletion}, timeout=0.2)
        assert done, "erasure did not bound its wait for shared state persistence"
        with pytest.raises(TimeoutError if outcome == "timeout" else asyncio.CancelledError):
            await deletion
        assert not writer.done(), "timed-out erasure must not cancel another owner's SQL write"
        assert current._persist_lock.locked(), "waiting erasure released the active writer's lock"
        assert state.get_user_state(42) is current
        assert current._user_id == 42
        assert database.db_manager._user_auth_cache == {42: True}
        assert erasure.epoch == 100
        erasure.release_retry.set()
        await asyncio.wait_for(writer, 3)
        assert erasure.commits == [(100, 5)]
        assert not current._persist_lock.locked()
    finally:
        erasure.release_retry.set()
        deletion.cancel()
        writer.cancel()
        await asyncio.gather(deletion, writer, return_exceptions=True)


async def test_admin_erasure_rejects_before_state_admission(erasure, monkeypatch):
    monkeypatch.setattr(users, "is_admin", lambda uid: True)

    def unexpected_state_lookup(uid):
        raise AssertionError("administrator rejection must precede persistence admission")

    monkeypatch.setattr(state, "get_user_state", unexpected_state_lookup)
    with pytest.raises(ValueError, match="administrator"):
        await users.erase_user_account(42)


async def test_commit_ack_cancellation_cannot_release_barrier_before_runtime_purge(erasure):
    erasure.retry_first = False
    erasure.pause_commit = True
    current = state.get_user_state(42)
    current.selected_document_id = 5
    assert await state._persist(current)
    owner = asyncio.create_task(users.erase_user_account(42))
    await erasure.commit_started.wait()
    assert erasure.epoch is None, "the destructive side effect must precede the interrupted acknowledgment"
    current.selected_document_id = 6
    queued = asyncio.create_task(state._persist(current))
    try:
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done(), "caller cancellation escaped before commit acknowledgment and purge"
        assert current._persist_lock.locked()
        assert not queued.done()
        erasure.release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert current._user_id == 0
        erasure.epoch = 200
        await queued
        assert erasure.commits == [(100, 5)]
    finally:
        erasure.release_commit.set()
        owner.cancel()
        queued.cancel()
        await asyncio.gather(owner, queued, return_exceptions=True)


async def test_unknown_commit_failure_invalidates_stale_state_without_acknowledging_erasure(erasure):
    erasure.retry_first = False
    erasure.commit_error = RuntimeError("synthetic unknown commit acknowledgment")
    current = state.get_user_state(42)
    current.selected_document_id = 5
    with pytest.raises(RuntimeError, match="unknown commit"):
        await users.erase_user_account(42)
    assert current._user_id == 0
    assert state.get_user_state(42) is not current
    assert not current._persist_lock.locked()
    assert 42 not in database.db_manager._user_auth_cache


@pytest.mark.parametrize("outcome", ["rollback", "cancel"])
async def test_failed_erasure_preserves_state_and_releases_persistence_barrier(erasure, monkeypatch, outcome):
    current = state.get_user_state(42)
    current._loaded_from_db = True
    current.role_diaries = {"role": ["synthetic saved note"]}
    erasure.retry_first = False
    entered = asyncio.Event()

    @asynccontextmanager
    async def privacy_barrier(*args, **kwargs):
        entered.set()
        if outcome == "cancel":
            await asyncio.Future()
        yield 12, True

    monkeypatch.setattr("app.repos.memory_consent.private_data_barrier", privacy_barrier)
    if outcome == "rollback":
        erasure.delete_error = asyncpg.PostgresError("synthetic rolled back deletion")
    owner = asyncio.create_task(users.erase_user_account(42))
    await entered.wait()
    if outcome == "cancel":
        owner.cancel()
    with pytest.raises(asyncio.CancelledError if outcome == "cancel" else asyncpg.PostgresError):
        await owner

    assert state.get_user_state(42) is current
    assert current._user_id == 42
    assert current.role_diaries == {"role": ["synthetic saved note"]}
    assert not current._persist_lock.locked()
    current.selected_document_id = 6
    assert await state._persist(current)
    assert erasure.commits == [(100, 6)]
