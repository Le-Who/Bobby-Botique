"""Owned private-data heartbeat and SQL release survive repeated cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repos import memory_consent


class _Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def execute(self, *_args):
        return None


async def _checkpoint():
    """Allow ready cancellation callbacks to run without a timing sleep."""
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["heartbeat", "release"])
async def test_repeated_cancellation_waits_for_heartbeat_and_exact_sql_release(monkeypatch, cancel_phase):
    """Unshielded gather/release lets the owner exit with its durable row alive."""
    heartbeat_started = asyncio.Event()
    heartbeat_cleanup_started = asyncio.Event()
    heartbeat_may_finish = asyncio.Event()
    heartbeat_finished = asyncio.Event()
    release_started = asyncio.Event()
    release_may_finish = asyncio.Event()
    release_finished = asyncio.Event()
    owner_entered = asyncio.Event()
    forever = asyncio.Event()
    heartbeat_tasks = []
    owned_id = None
    foreign_id = UUID("00000000-0000-0000-0000-000000000011")
    newer_id = UUID("00000000-0000-0000-0000-000000000012")
    rows = {foreign_id: (99, 7), newer_id: (42, 9)}
    deletes = []

    async def context(*_args, **_kwargs):
        return None

    async def query(sql, params=(), *, conn):
        nonlocal owned_id
        if "INSERT INTO public.private_data_leases" in sql:
            owned_id = params[1]
            rows[owned_id] = (params[0], params[3])
            return [{"lease_id": owned_id}]
        assert "DELETE FROM public.private_data_leases WHERE user_id = $1 AND lease_id = $2" in sql
        deletes.append(params)
        release_started.set()
        await release_may_finish.wait()
        if rows.get(params[1], (None,))[0] == params[0]:
            rows.pop(params[1], None)
        release_finished.set()
        return []

    async def renew(_user_id, _lease_id):
        heartbeat_tasks.append(asyncio.current_task())
        heartbeat_started.set()
        try:
            await forever.wait()
        finally:
            heartbeat_cleanup_started.set()
            await heartbeat_may_finish.wait()
            heartbeat_finished.set()

    async def owner():
        async with memory_consent.private_data_lease(42, 7, purpose="ltm:test", require_ltm=True) as allowed:
            assert allowed
            owner_entered.set()
            await forever.wait()

    monkeypatch.setattr(memory_consent, "db_manager", SimpleNamespace(pool=_Context(), is_connected=True))
    monkeypatch.setattr(memory_consent, "set_user_context", context)
    monkeypatch.setattr(memory_consent, "clear_user_context", context)
    monkeypatch.setattr(memory_consent, "db_query", query)
    monkeypatch.setattr(memory_consent, "_renew_private_data_lease", renew)
    monkeypatch.setattr(memory_consent, "_LEASE_HEARTBEAT_SECONDS", 0)
    task = asyncio.create_task(owner())
    try:
        await owner_entered.wait()
        await heartbeat_started.wait()
        task.cancel("initial cancellation")
        await heartbeat_cleanup_started.wait()
        if cancel_phase == "release":
            heartbeat_may_finish.set()
            await release_started.wait()
        task.cancel("repeated cancellation")
        await _checkpoint()
        await _checkpoint()
        assert not task.done(), "owner must retain cleanup ownership under repeated cancellation"
        assert owned_id in rows
        heartbeat_may_finish.set()
        await release_started.wait()
        assert heartbeat_finished.is_set()
        task.cancel("third cancellation during release")
        await _checkpoint()
        await _checkpoint()
        assert not task.done()
        release_may_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert release_finished.is_set()
        assert rows == {foreign_id: (99, 7), newer_id: (42, 9)}
        assert deletes == [(42, owned_id)]
        assert all(heartbeat.done() for heartbeat in heartbeat_tasks)
    finally:
        heartbeat_may_finish.set()
        release_may_finish.set()
        forever.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *heartbeat_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_first_arriving_during_normal_release_remains_cancellation(monkeypatch):
    """Shielding cleanup must not turn a canceled successful body into success."""
    body_finished = asyncio.Event()
    release_started = asyncio.Event()
    release_may_finish = asyncio.Event()
    release_finished = asyncio.Event()

    async def acquire(*_args):
        return True

    async def release(*_args):
        release_started.set()
        await release_may_finish.wait()
        release_finished.set()

    async def owner():
        async with memory_consent.private_data_lease(42, 7, purpose="ltm:test", require_ltm=True) as allowed:
            assert allowed
            body_finished.set()
        return "completed"

    monkeypatch.setattr(memory_consent, "_acquire_private_data_lease", acquire)
    monkeypatch.setattr(memory_consent, "_release_private_data_lease", release)
    task = asyncio.create_task(owner())
    try:
        await release_started.wait()
        assert body_finished.is_set()
        task.cancel("cancellation during normal release")
        await _checkpoint()
        assert not task.done()
        release_may_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert release_finished.is_set()
    finally:
        release_may_finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
