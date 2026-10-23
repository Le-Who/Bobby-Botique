"""Album admission and dispatcher ownership without Telegram/provider traffic."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers import agent, msg_media
from app.utils.background_tasks import TaskManager


@pytest.mark.asyncio
async def test_single_photo_album_reaches_real_dispatcher_with_effective_message(monkeypatch):
    sender = SimpleNamespace(id=42)
    message = SimpleNamespace(
        from_user=sender, chat=SimpleNamespace(id=42), photo=[object()], text=None, caption="look"
    )
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    context = SimpleNamespace(user_data={})
    monkeypatch.setattr(msg_media, "MEDIA_GROUPS", {"one": {"messages": [message], "placeholder_message": placeholder}})
    monkeypatch.setattr(
        agent, "get_user_chat", AsyncMock(return_value=SimpleNamespace(search_enabled=False, is_deep_dive=False))
    )
    processed = []

    @asynccontextmanager
    async def capacity():
        yield

    async def analyze(_placeholder, original, _chat):
        processed.append(original)

    monkeypatch.setattr(agent, "heavy_request_semaphore", capacity())
    monkeypatch.setattr(agent, "_handle_photo", analyze)
    await msg_media._process_single_image_from_group("one", context)

    assert processed == [message]
    placeholder.edit_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_start", [False, True])
async def test_album_worker_is_drained_with_tracked_background_work(monkeypatch, cancel_before_start):
    manager = TaskManager()
    worker_started = asyncio.Event()
    worker_finished = asyncio.Event()
    sender = SimpleNamespace(id=42)
    placeholder = SimpleNamespace(message_id=100)
    message = SimpleNamespace(
        from_user=sender,
        chat=SimpleNamespace(id=42),
        photo=[object()],
        forward_origin=None,
        media_group_id="synthetic-album",
        caption=None,
        reply_text=AsyncMock(return_value=placeholder),
    )
    monkeypatch.setattr(msg_media, "MEDIA_GROUPS", {})
    monkeypatch.setattr(msg_media, "MEDIA_GROUPS_TTL", {})
    monkeypatch.setattr(msg_media, "_background_tasks", set())
    monkeypatch.setattr(msg_media, "_cleanup_task", None)
    monkeypatch.setattr(msg_media, "start_media_groups_cleanup", AsyncMock())
    monkeypatch.setattr(msg_media, "submit_task", manager.submit)

    async def process(_group_id, _context):
        worker_started.set()
        try:
            await asyncio.Future()
        finally:
            worker_finished.set()

    monkeypatch.setattr(msg_media, "_process_single_image_from_group", process)
    try:
        assert await msg_media.process_media_group_update(SimpleNamespace(message=message), object(), 42, 42) is True
        if cancel_before_start:
            owner = next(iter(msg_media._background_tasks))
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
        else:
            await worker_started.wait()
            assert await manager.drain(timeout=0) is True

        assert worker_finished.is_set() is not cancel_before_start
        assert not msg_media.MEDIA_GROUPS
        assert not msg_media._background_tasks
    finally:
        pending = list(msg_media._background_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await manager.drain(timeout=0)


@pytest.mark.asyncio
async def test_album_maintenance_owner_is_registered_before_admission_returns(monkeypatch):
    manager = TaskManager()
    message = SimpleNamespace(
        photo=[object()],
        forward_origin=None,
        media_group_id="owned-loop",
        caption=None,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    monkeypatch.setattr(msg_media, "MEDIA_GROUPS", {})
    monkeypatch.setattr(msg_media, "MEDIA_GROUPS_TTL", {})
    monkeypatch.setattr(msg_media, "_background_tasks", set())
    monkeypatch.setattr(msg_media, "_cleanup_task", None)
    monkeypatch.setattr(msg_media, "submit_task", manager.submit)
    try:
        await msg_media.process_media_group_update(SimpleNamespace(message=message), object(), 42, 42)
        assert msg_media._cleanup_task is not None
        await msg_media.stop_media_groups_cleanup()
        await manager.drain(timeout=0)
        assert msg_media._cleanup_task is None
    finally:
        await manager.drain(timeout=0)
        await msg_media.stop_media_groups_cleanup()
