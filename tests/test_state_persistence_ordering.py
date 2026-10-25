"""Persist real state snapshots through the repository and a synthetic SQL driver."""

import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app import database, state

pytestmark = pytest.mark.asyncio


class StateDriver:
    """Atomic fake fetches; the real database manager owns retry/cancellation."""

    _closed = False

    def __init__(self):
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.retry_first = False
        self.attempts = []
        self.commits = []
        self.rows = {}
        self.acquired = 0
        self.released = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquired += 1
        try:
            yield self
        finally:
            self.released += 1

    async def fetch(self, sql, *params):
        assert "INSERT INTO user_state" in sql
        user_id, _document_mode, document_id = params[:3]
        self.attempts.append((user_id, document_id))
        if (user_id, document_id) == (101, 41) and not self.first_started.is_set():
            self.first_started.set()
            if self.retry_first:
                raise asyncpg.DeadlockDetectedError("synthetic aborted state write")
            await self.release_first.wait()
        self.rows[user_id] = params
        self.commits.append((user_id, document_id))
        return []


@pytest.fixture
async def driver(monkeypatch):
    driver = StateDriver()
    monkeypatch.setattr(database.db_manager, "pool", driver)
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=1))
    monkeypatch.setattr(state, "_pending_persists", {})
    tasks = []

    def persist(user_state):
        task = asyncio.create_task(state._persist(user_state))
        tasks.append(task)
        return task

    driver.persist = persist
    yield driver
    driver.release_first.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for timer in state._pending_persists.values():
        timer.cancel()
    assert driver.acquired == driver.released


def selected_state(user_id, document_id):
    user_state = state.get_user_state(user_id)
    user_state._loaded_from_db = True
    user_state.document_mode = True
    user_state.selected_document_id = document_id
    return user_state


async def test_retried_old_snapshot_cannot_overwrite_the_latest_state_after_eviction(driver):
    driver.retry_first = True
    original = selected_state(101, 41)
    # The request lock is independent: persistence must not wait on a handler.
    async with original.lock:
        first = driver.persist(original)
        await asyncio.wait_for(driver.first_started.wait(), 1)
        state.get_user_state(202)
        latest = state.get_user_state(101)
        assert latest is original
        latest.selected_document_id = 42
        second = driver.persist(latest)
        await asyncio.wait_for(asyncio.gather(first, second), 3)

    assert driver.attempts == [(101, 41), (101, 41), (101, 42)]
    assert driver.commits == [(101, 41), (101, 42)]
    assert driver.rows[101][2] == 42
    assert state.get_selected_document_id(101) == 42


async def test_slow_state_persistence_does_not_block_another_user(driver):
    original = selected_state(101, 41)
    first = driver.persist(original)
    await asyncio.wait_for(driver.first_started.wait(), 1)
    original.selected_document_id = 42
    queued = driver.persist(original)
    other = selected_state(202, 99)

    await asyncio.wait_for(driver.persist(other), 1)

    assert driver.rows[202][2] == 99
    assert not first.done()
    driver.release_first.set()
    await asyncio.wait_for(asyncio.gather(first, queued), 1)
    assert driver.rows[101][2] == 42


async def test_cancelled_active_persist_propagates_and_releases_the_next_writer(driver):
    original = selected_state(101, 41)
    first = driver.persist(original)
    await asyncio.wait_for(driver.first_started.wait(), 1)
    original.selected_document_id = 42
    queued = driver.persist(original)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(queued, 1)

    assert driver.commits == [(101, 42)]
    assert driver.rows[101][2] == 42


async def test_queued_pre_erasure_persist_rechecks_identity_before_saving(driver):
    original = selected_state(101, 41)
    first = driver.persist(original)
    await asyncio.wait_for(driver.first_started.wait(), 1)
    original.selected_document_id = 42
    queued_started = asyncio.Event()

    async def queued_persist():
        queued_started.set()
        await state._persist(original)

    queued = asyncio.create_task(queued_persist())
    try:
        await asyncio.wait_for(queued_started.wait(), 1)
        state.purge_user_runtime_state(101)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(queued, 1)

        assert driver.commits == []
        assert driver.rows == {}
        assert original._user_id == 0
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
