"""Controlled timer/STT schedules exercise real debounce ownership (ST-05)."""

import asyncio
from types import SimpleNamespace

import pytest

from app.middleware import debounce
from app.utils.background_tasks import TaskManager

pytestmark = pytest.mark.asyncio


def message(text="", *, voice=None, photo=(), forwarded=False):
    return SimpleNamespace(
        text=text,
        caption=None,
        voice=voice,
        photo=photo,
        forward_origin=SimpleNamespace(type="hidden_user", sender_user_name="Author", date=None) if forwarded else None,
    )


@pytest.fixture
async def slots(monkeypatch):
    monkeypatch.setattr(debounce, "_debounce_slots", {})
    monkeypatch.setattr(debounce, "_DEFAULT_WINDOW_S", 60)
    monkeypatch.setattr(debounce, "_FORWARD_WINDOW_S", 120)
    manager = TaskManager()
    monkeypatch.setattr(debounce, "submit_task", manager.submit)
    yield
    for slot in debounce._debounce_slots.values():
        if slot.timer_task:
            slot.timer_task.cancel()
            await asyncio.gather(slot.timer_task, return_exceptions=True)
    await manager.drain(timeout=0)


async def next_turn():
    """A scheduler barrier, with no wall-clock delay."""
    await asyncio.sleep(0)


@pytest.mark.parametrize("legacy", [False, True], ids=["message", "legacy"])
async def test_cancelled_owner_removes_only_its_slot_and_awaits_timer(slots, legacy):
    owner = asyncio.create_task(
        debounce.debounce_text_message(11, "first") if legacy else debounce.debounce_message(11, message("first"))
    )
    try:
        await next_turn()
        if owner.done():
            await owner
        slot = debounce._debounce_slots[11]
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        assert 11 not in debounce._debounce_slots
        assert slot.timer_task.done()
        assert slot.timer_task.cancelled()
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_trailing_restart_flushes_text_forward_and_photo_once(slots, monkeypatch):
    timer_waits = asyncio.Queue()
    real_sleep = asyncio.sleep

    async def controlled_sleep(delay):
        if delay in (60, 120):
            timer_waits.put_nowait(delay)
            await asyncio.Future()
        else:
            await real_sleep(delay)

    monkeypatch.setattr(debounce.asyncio, "sleep", controlled_sleep)
    first = asyncio.create_task(debounce.debounce_message(11, message("question")))
    try:
        assert await asyncio.wait_for(timer_waits.get(), 1) == 60
        slot = debounce._debounce_slots[11]
        old_timer = slot.timer_task
        assert await debounce.debounce_message(11, message("forward", forwarded=True)) is None
        assert await asyncio.wait_for(timer_waits.get(), 1) == 120
        assert slot.is_forward_burst
        assert slot.timer_task is not old_timer
        forwarded_photo = message(photo=(SimpleNamespace(file_id="photo-1"),), forwarded=True)
        forwarded_photo.caption = "photo"
        assert await debounce.inject_forwarded_photo(11, forwarded_photo)
        timer = slot.timer_task
        slot.ready_event.set()
        result = await first
        assert [entry.text for entry in result.entries] == ["question", "forward", "photo"]
        assert len(result.user_entries) == 1
        assert len(result.forwarded_entries) == 2
        assert result.forwarded_photo_messages == [forwarded_photo]
        assert result.has_mixed
        assert old_timer.done() and timer.done()
        assert not debounce.has_open_slot(11)
        assert not await debounce.inject_forwarded_photo(11, message("late", forwarded=True))
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_old_stt_owner_does_not_remove_replacement_burst(slots, monkeypatch):
    stt_started = asyncio.Event()
    release_stt = asyncio.Event()

    async def transcribe(msg, future):
        stt_started.set()
        await release_stt.wait()
        future.set_result("transcript")

    monkeypatch.setattr(debounce, "_transcribe_forwarded_voice", transcribe)
    old = asyncio.create_task(debounce.debounce_message(11, message(voice=object(), forwarded=True)))
    replacement = None
    try:
        await stt_started.wait()
        old_slot = debounce._debounce_slots[11]
        old_slot.ready_event.set()
        await next_turn()
        replacement = asyncio.create_task(debounce.debounce_message(11, message("second burst")))
        await next_turn()
        new_slot = debounce._debounce_slots[11]
        assert new_slot is not old_slot
        release_stt.set()
        result = await old
        assert [entry.text for entry in result.entries] == ["transcript"]
        assert debounce._debounce_slots[11] is new_slot
        assert await debounce.debounce_message(11, message("absorbed")) is None
        new_slot.ready_event.set()
        result = await replacement
        assert [entry.text for entry in result.entries] == ["second burst", "absorbed"]
        assert not debounce._debounce_slots
    finally:
        release_stt.set()
        for task in (old, replacement):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_cancel_during_stt_wait_finishes_owned_transcription(slots, monkeypatch):
    stt_started = asyncio.Event()
    stt_finished = asyncio.Event()
    tasks = []

    async def transcribe(msg, future):
        tasks.append(asyncio.current_task())
        stt_started.set()
        try:
            await asyncio.Future()
        finally:
            await next_turn()
            stt_finished.set()

    monkeypatch.setattr(debounce, "_transcribe_forwarded_voice", transcribe)
    owner = asyncio.create_task(debounce.debounce_message(11, message(voice=object(), forwarded=True)))
    try:
        await stt_started.wait()
        slot = debounce._debounce_slots[11]
        slot.ready_event.set()
        await next_turn()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        assert stt_finished.is_set()
        assert all(task.done() for task in tasks)
        assert not debounce._debounce_slots
        assert slot.timer_task.done()
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.parametrize("flush_slot", [False, True], ids=["window", "stt_wait"])
async def test_repeated_owner_cancel_keeps_stt_cleanup_owned(slots, monkeypatch, flush_slot):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    tasks = []

    async def transcribe(msg, future):
        tasks.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    monkeypatch.setattr(debounce, "_transcribe_forwarded_voice", transcribe)
    owner = asyncio.create_task(debounce.debounce_message(11, message(voice=object(), forwarded=True)))
    try:
        await asyncio.wait_for(started.wait(), 1)
        slot = debounce._debounce_slots[11]
        if flush_slot:
            slot.ready_event.set()
            await next_turn()
        owner.cancel("original shutdown")
        await asyncio.wait_for(cleanup_started.wait(), 1)
        for _ in range(2):
            owner.cancel("repeated shutdown")
            checkpoint = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(checkpoint.set_result, None)
            await checkpoint
            assert not owner.done(), "the owner must await STT cleanup through repeated cancellation"
            assert not cleanup_finished.is_set()
        assert not debounce._debounce_slots
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("original shutdown",)
        assert cleanup_finished.is_set()
        assert all(task.done() for task in tasks)
        assert slot.timer_task.done()
        assert slot.entries[0].stt_future.cancelled()
    finally:
        release_cleanup.set()
        for task in [owner, *tasks]:
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, *tasks, return_exceptions=True)


async def test_stt_grace_stub_returns_once_and_late_transcript_finishes(slots, monkeypatch):
    started = asyncio.Event()
    finish = asyncio.Event()
    tasks = []

    async def transcribe(msg, future):
        tasks.append(asyncio.current_task())
        started.set()
        await finish.wait()
        future.set_result("late transcript")

    monkeypatch.setattr(debounce, "_transcribe_forwarded_voice", transcribe)
    monkeypatch.setattr(debounce, "_STT_GRACE_S", 0)
    owner = asyncio.create_task(debounce.debounce_message(11, message(voice=object(), forwarded=True)))
    try:
        await started.wait()
        slot = debounce._debounce_slots[11]
        slot.ready_event.set()
        result = await owner
        assert [entry.text for entry in result.entries] == ["[Голосовое сообщение — расшифровка обрабатывается]"]
        assert not slot.entries[0].stt_future.cancelled()
        assert not debounce._debounce_slots
        finish.set()
        await asyncio.gather(*tasks)
        assert [entry.text for entry in result.entries] == ["[Голосовое сообщение — расшифровка обрабатывается]"]
    finally:
        finish.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
