"""Canvas generation retains request admission through all task outcomes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import state
from app.handlers import cb_image
from app.utils.background_tasks import TaskManager


@pytest.fixture
async def draw_manager(monkeypatch):
    manager = TaskManager()
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore())
    monkeypatch.setattr(state, "_ACTIVE_TASKS", {})
    monkeypatch.setattr("app.utils.background_tasks.submit_task", manager.submit)
    monkeypatch.setattr("app.utils.background_tasks.try_submit_task", manager.try_submit)
    yield manager
    await manager.drain(timeout=0)


def request(action):
    user = SimpleNamespace(id=913)
    query = SimpleNamespace(
        data=f"draw:{action}",
        from_user=user,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_user=user)
    context = SimpleNamespace(
        user_data={
            "draw_state": {
                "prompt": "Synthetic prompt",
                "model": "zimage",
                "aspect_ratio": "1:1",
                "enhance_prompt": False,
            }
        }
    )
    return update, context


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["execute", "regen"])
@pytest.mark.parametrize("reason", ["capacity", "shutdown"])
async def test_rejected_canvas_generation_releases_busy_state(draw_manager, monkeypatch, action, reason):
    if reason == "capacity":
        draw_manager.MAX_TASKS = 0
    else:

        async def pending_work():
            await asyncio.Future()

        draw_manager.try_submit(pending_work())
        await asyncio.sleep(0)
        assert await draw_manager.drain(timeout=0)
        assert draw_manager.metrics_snapshot()["accepting"] is False
    generation = AsyncMock()
    monkeypatch.setattr("app.handlers.cmd_image._run_generation", generation)
    update, context = request(action)
    await cb_image.draw_callback(update, context)
    await asyncio.sleep(0)
    assert not state.get_user_state(913).is_processing
    assert state.get_active_task(913) is None
    generation.assert_not_awaited()
    assert draw_manager.metrics_snapshot()["rejected_total"] == 1
    assert len(update.callback_query.answer.await_args_list) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["execute", "regen"])
async def test_canvas_cancel_before_first_step_releases_admission(draw_manager, monkeypatch, action):
    generation = AsyncMock()
    monkeypatch.setattr("app.handlers.cmd_image._run_generation", generation)
    update, context = request(action)
    await cb_image.draw_callback(update, context)
    (task,) = tuple(draw_manager._tasks)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not state.get_user_state(913).is_processing
    assert state.get_active_task(913) is None
    generation.assert_not_awaited()


@pytest.mark.asyncio
async def test_canvas_worker_keeps_owner_until_cancel_cleanup_finishes(draw_manager, monkeypatch):
    started, cleaning, release = (asyncio.Event() for _ in range(3))

    async def generate(**kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    monkeypatch.setattr("app.handlers.cmd_image._run_generation", generate)
    update, context = request("execute")
    await cb_image.draw_callback(update, context)
    await asyncio.wait_for(started.wait(), 1)
    (task,) = tuple(draw_manager._tasks)
    try:
        assert state.get_active_task(913) is task
        assert state.cancel_active_task(913)
        await asyncio.wait_for(cleaning.wait(), 1)
        assert state.cancel_active_task(913)
        assert state.get_user_state(913).is_processing
        assert not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not state.get_user_state(913).is_processing
        assert state.get_active_task(913) is None
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_canvas_cleanup_cannot_release_a_newer_owner(draw_manager):
    update, context = request("execute")
    await cb_image.draw_callback(update, context)
    (old,) = tuple(draw_manager._tasks)
    old.cancel()

    async def hold():
        await asyncio.Future()

    newer = asyncio.create_task(hold())
    state.register_active_task(913, newer)
    try:
        await asyncio.gather(old, return_exceptions=True)
        await asyncio.sleep(0)
        assert state.get_active_task(913) is newer
        assert state.get_user_state(913).is_processing
    finally:
        newer.cancel()
        await asyncio.gather(newer, return_exceptions=True)
        state.clear_active_task(913)
        state.get_user_state(913).is_processing = False


@pytest.mark.asyncio
async def test_canvas_worker_drains_prior_role_work_before_generating(draw_manager, monkeypatch):
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    monkeypatch.setattr(state, "_schedule_persist", lambda current: None)
    owner = state.begin_interaction(913, "role")

    async def role_work():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    old = asyncio.create_task(state.run_interaction(owner, role_work))
    generation = AsyncMock(return_value=True)
    monkeypatch.setattr("app.handlers.cmd_image._run_generation", generation)
    update, context = request("execute")
    try:
        await asyncio.wait_for(started.wait(), 1)
        await cb_image.draw_callback(update, context)
        (worker,) = tuple(draw_manager._tasks)
        await asyncio.sleep(0)
        generation.assert_not_awaited()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert state.get_user_state(913).is_processing
        assert not worker.done()
        release.set()
        await asyncio.gather(old, worker)
        generation.assert_awaited_once()
        assert state.get_user_state(913).interaction is None
    finally:
        release.set()
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)
