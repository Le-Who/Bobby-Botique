"""Per-user memory cancellation retains ownership of asynchronous child cleanup."""

import asyncio

import pytest

from app.repos import memory_autosave as autosave

pytestmark = pytest.mark.asyncio


async def _checkpoint() -> None:
    checkpoint = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
    await checkpoint


@pytest.fixture
def memory_tasks(monkeypatch):
    monkeypatch.setattr(autosave, "_inflight_memory_tasks", set())
    monkeypatch.setattr(autosave, "_inflight_memory_tasks_by_user", {})


@pytest.mark.parametrize("cleanup_failure", [False, True], ids=["cleanup_success", "cleanup_failure"])
@pytest.mark.parametrize("caller_cancellations", [0, 1, 3], ids=["ordinary", "caller_cancel", "repeated_cancel"])
async def test_user_cancellation_awaits_cleanup_and_preserves_other_users(
    memory_tasks, caller_cancellations, cleanup_failure
):
    child_started = asyncio.Event()
    unrelated_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    cleanup_interrupted = asyncio.Event()
    cleanup_attempts = []

    async def child_write():
        child_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_attempts.append("started")
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                cleanup_interrupted.set()
                raise
            cleanup_finished.set()
            if cleanup_failure:
                raise RuntimeError("synthetic child cleanup failure")

    async def unrelated_write():
        unrelated_started.set()
        await asyncio.Event().wait()

    child = asyncio.create_task(child_write())
    unrelated = asyncio.create_task(unrelated_write())
    autosave.register_memory_task(child, user_id=11)
    autosave.register_memory_task(unrelated, user_id=12)
    await child_started.wait()
    await unrelated_started.wait()
    owner = asyncio.create_task(autosave.cancel_user_memory_tasks(11))
    # A registered cancellation caller must exclude itself from the child set.
    autosave.register_memory_task(owner, user_id=11)

    try:
        await cleanup_started.wait()
        assert not owner.done(), "erasure caller must await its child's pending cleanup"
        assert not child.done()
        assert not unrelated.done()

        for index in range(caller_cancellations):
            owner.cancel("original erase cancellation" if index == 0 else f"repeated erase cancellation {index}")
            await _checkpoint()
            assert not cleanup_interrupted.is_set(), "caller cancellation must not interrupt child cleanup"
            assert not owner.done(), "erasure caller must retain ownership until child cleanup finishes"
            assert not child.done()
            assert not cleanup_finished.is_set()
            assert not unrelated.done()

        release_cleanup.set()
        if caller_cancellations:
            with pytest.raises(asyncio.CancelledError) as cancellation:
                await owner
            assert cancellation.value.args == ("original erase cancellation",)
        else:
            assert await owner == 1

        assert cleanup_finished.is_set()
        assert cleanup_attempts == ["started"]
        assert child.done()
        if cleanup_failure:
            assert isinstance(child.exception(), RuntimeError)
            assert str(child.exception()) == "synthetic child cleanup failure"
        else:
            assert child.cancelled()
        assert not unrelated.done()
        assert autosave._inflight_memory_tasks == {unrelated}
        assert autosave._inflight_memory_tasks_by_user == {12: {unrelated}}
    finally:
        release_cleanup.set()
        # Let owned cleanup finish before teardown sends any further cancellation.
        await _checkpoint()
        for task in (owner, child, unrelated):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, child, unrelated, return_exceptions=True)
