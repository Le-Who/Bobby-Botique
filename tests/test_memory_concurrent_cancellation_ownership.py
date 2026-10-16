"""Overlapping memory cancellation owners share one uninterrupted child cleanup."""

import asyncio

import pytest

from app.repos import memory_autosave as autosave
from app.utils import background_tasks

pytestmark = pytest.mark.asyncio


async def _checkpoint() -> None:
    checkpoint = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
    await checkpoint


@pytest.fixture
def memory_tasks(monkeypatch):
    monkeypatch.setattr(autosave, "_inflight_memory_tasks", set())
    monkeypatch.setattr(autosave, "_inflight_memory_tasks_by_user", {})
    manager = background_tasks.TaskManager()
    monkeypatch.setattr(background_tasks, "submit_retryable", manager.submit_retryable)
    return manager


@pytest.mark.parametrize("submission", ["registered", "task_manager"])
@pytest.mark.parametrize("second_owner", ["user", "drain"])
@pytest.mark.parametrize("cleanup_failure", [False, True], ids=["cleanup_success", "cleanup_failure"])
@pytest.mark.parametrize(
    "cancelled_owners",
    [(), ("first",), ("second",), ("first", "second")],
    ids=["ordinary", "cancel_first", "cancel_second", "cancel_both"],
)
async def test_overlapping_owners_join_one_uninterrupted_cleanup(
    monkeypatch, memory_tasks, submission, second_owner, cleanup_failure, cancelled_owners
):
    tasks_before = asyncio.all_tasks()
    child_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    cleanup_interrupted = asyncio.Event()
    second_join_started = asyncio.Event()
    cleanup_attempts = []
    joined_children = {}
    owners = {}
    unrelated = None
    original_helper = autosave._cancel_and_await

    async def observe_join(tasks, **kwargs):
        owner = asyncio.current_task()
        joined_children[owner] = tuple(tasks)
        if owner is owners.get("second"):
            second_join_started.set()
        await original_helper(tasks, **kwargs)

    monkeypatch.setattr(autosave, "_cancel_and_await", observe_join)

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

    if submission == "task_manager":
        child = autosave.submit_memory_task(11, child_write, retry=0)
        assert child in memory_tasks._tasks
    else:
        child = asyncio.create_task(child_write())
        autosave.register_memory_task(child, user_id=11)

    # Only per-user owners promise to preserve another user's work.
    if second_owner == "user":
        unrelated = asyncio.create_task(asyncio.Event().wait())
        autosave.register_memory_task(unrelated, user_id=12)

    await child_started.wait()
    owners["first"] = asyncio.create_task(autosave.cancel_user_memory_tasks(11))
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert child.cancelling() == 1, "the first owner must promptly initiate child cancellation"
        owners["second"] = asyncio.create_task(
            autosave.cancel_user_memory_tasks(11)
            if second_owner == "user"
            else autosave.drain_pending_memory_writes(timeout=0)
        )
        await asyncio.wait_for(second_join_started.wait(), 1)
        await _checkpoint()

        def assert_cleanup_owned():
            assert not cleanup_interrupted.is_set(), "a second owner must not interrupt the child's finalizer"
            assert child.cancelling() == 1, "overlapping owners must initiate cancellation only once"
            assert cleanup_attempts == ["started"]
            assert not cleanup_finished.is_set()
            assert not release_cleanup.is_set()
            assert not child.done()
            assert all(not owner.done() for owner in owners.values())
            assert child in autosave._inflight_memory_tasks
            assert autosave._inflight_memory_tasks_by_user[11] == {child}
            if unrelated is not None:
                assert not unrelated.done()
                assert unrelated.cancelling() == 0

        assert joined_children == {owners["first"]: (child,), owners["second"]: (child,)}
        assert_cleanup_owned()
        for index in range(3):
            for name in cancelled_owners:
                owners[name].cancel(f"{name} owner cancellation" if index == 0 else f"{name} repeated {index}")
                await _checkpoint()
                assert_cleanup_owned()

        release_cleanup.set()
        for name, owner in owners.items():
            if name in cancelled_owners:
                with pytest.raises(asyncio.CancelledError) as cancellation:
                    await owner
                assert cancellation.value.args == (f"{name} owner cancellation",)
            else:
                assert await owner == (None if name == "second" and second_owner == "drain" else 1)

        assert cleanup_finished.is_set()
        assert cleanup_attempts == ["started"]
        assert not cleanup_interrupted.is_set()
        assert child.done()
        if cleanup_failure and submission == "registered":
            assert isinstance(child.exception(), RuntimeError)
            assert str(child.exception()) == "synthetic child cleanup failure"
        elif cleanup_failure:
            assert child.result() is None, "TaskManager contains exhausted worker failures"
        else:
            assert child.cancelled()
        await _checkpoint()
        remaining = {unrelated} if unrelated is not None else set()
        assert autosave._inflight_memory_tasks == remaining
        assert autosave._inflight_memory_tasks_by_user == ({12: remaining} if remaining else {})
        assert not memory_tasks._tasks
        assert asyncio.all_tasks() == tasks_before | remaining, "every completion join must finish with its owners"
    finally:
        release_cleanup.set()
        await asyncio.gather(child, *owners.values(), return_exceptions=True)
        if unrelated is not None:
            unrelated.cancel()
            await asyncio.gather(unrelated, return_exceptions=True)


async def test_user_owner_joins_already_cancelling_child_without_interrupting_cleanup(memory_tasks):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def child_write():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    child = asyncio.create_task(child_write())
    autosave.register_memory_task(child, user_id=11)
    await started.wait()
    child.cancel("existing child cancellation")
    await cleanup_started.wait()
    owner = asyncio.create_task(autosave.cancel_user_memory_tasks(11))
    try:
        await _checkpoint()
        assert child.cancelling() == 1, "joining an existing cancellation must not send a second signal"
        assert not child.done()
        assert not owner.done()
        assert autosave._inflight_memory_tasks_by_user == {11: {child}}
        release_cleanup.set()
        assert await owner == 1
        assert cleanup_finished.is_set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await child
        assert cancellation.value.args == ("existing child cancellation",)
        assert not autosave._inflight_memory_tasks
        assert not autosave._inflight_memory_tasks_by_user
    finally:
        release_cleanup.set()
        await asyncio.gather(owner, child, return_exceptions=True)


async def test_completed_child_keeps_result_and_needs_no_cancellation(memory_tasks):
    async def completed_write():
        return 17

    child = asyncio.create_task(completed_write())
    assert await child == 17
    autosave.register_memory_task(child, user_id=11)
    assert await autosave.cancel_user_memory_tasks(11) == 0
    await autosave._cancel_and_await([child])
    await _checkpoint()
    assert child.result() == 17
    assert child.cancelling() == 0
    assert not autosave._inflight_memory_tasks
    assert not autosave._inflight_memory_tasks_by_user
