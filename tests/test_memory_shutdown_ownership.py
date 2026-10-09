"""Direct shutdown utilities own cancellation through asynchronous cleanup (ST-04)."""

import asyncio

import pytest

from app.repos import memory_autosave as autosave

pytestmark = pytest.mark.asyncio


@pytest.fixture
def memory_tasks(monkeypatch):
    monkeypatch.setattr(autosave, "_inflight_memory_tasks", set())
    monkeypatch.setattr(autosave, "_inflight_memory_tasks_by_user", {})


@pytest.mark.parametrize("utility", ["drain", "compact"])
@pytest.mark.parametrize("cancel_owner", [False, True], ids=["timeout", "owner_cancel"])
@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["single", "repeated"])
async def test_shutdown_utility_awaits_child_finally(monkeypatch, memory_tasks, utility, cancel_owner, repeat_cancel):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    children = []

    async def worker(*args):
        children.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    if utility == "compact":
        monkeypatch.setattr("app.repos.memory_consolidation._consolidation_state", {11: {"msg_count": 100}})
        monkeypatch.setattr("app.repos.memory_consolidation.maybe_consolidate", worker)

        async def key(**kwargs):
            return {"api_key": "synthetic-key"}

        monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
        owner = asyncio.create_task(autosave.pre_shutdown_compact(timeout=30 if cancel_owner else 0))
    else:
        child = asyncio.create_task(worker())
        autosave.register_memory_task(child, user_id=11)
        await started.wait()
        drain_started = asyncio.Event()

        async def drain():
            drain_started.set()
            await autosave.drain_pending_memory_writes(timeout=30 if cancel_owner else 0)

        owner = asyncio.create_task(drain())
        await drain_started.wait()

    try:
        await asyncio.wait_for(started.wait(), 1)
        if cancel_owner:
            owner.cancel("original shutdown")
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not owner.done(), "shutdown must retain ownership while rollback/lease release is pending"
        if repeat_cancel:
            for index in range(2):
                owner.cancel(f"repeated shutdown {index}")
                # A queued checkpoint lets the cancellation run before we
                # examine ownership; the worker's cleanup remains barriered.
                checkpoint = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
                await checkpoint
                assert not owner.done(), "repeated caller cancellation must keep asynchronous cleanup owned"
                assert not cleanup_finished.is_set()
        release_cleanup.set()
        if cancel_owner or repeat_cancel:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await owner
            assert cancelled.value.args == ("original shutdown" if cancel_owner else "repeated shutdown 0",)
        elif utility == "compact":
            assert await owner == 0, "timed-out candidates do not count as completed compact attempts"
        else:
            assert await owner is None
        assert cleanup_finished.is_set()
        assert all(task.done() for task in children)
        assert not autosave._inflight_memory_tasks
        assert not autosave._inflight_memory_tasks_by_user
    finally:
        release_cleanup.set()
        for task in [owner, *children]:
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, *children, return_exceptions=True)


async def test_compact_counts_completed_attempts_and_contains_candidate_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.repos.memory_consolidation._consolidation_state",
        {11: {"msg_count": 100}, 12: {"msg_count": 100}, 13: {"msg_count": 0}},
    )

    async def key(**kwargs):
        return {"api_key": "synthetic-key"}

    async def compact(uid, key):
        calls.append(uid)
        if uid == 12:
            raise RuntimeError("synthetic consolidation fault")
        return 2

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", key)
    monkeypatch.setattr("app.repos.memory_consolidation.maybe_consolidate", compact)
    assert await autosave.pre_shutdown_compact(timeout=1) == 2
    assert sorted(calls) == [11, 12]
