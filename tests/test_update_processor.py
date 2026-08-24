import asyncio
import inspect
from dataclasses import dataclass

import pytest
from telegram.ext import BaseUpdateProcessor

from app.update_processor import UserScopedUpdateProcessor


@dataclass(frozen=True)
class _User:
    id: int


class _Update:
    def __init__(self, user_id: int | None, chat_id: int = 1):
        self.effective_user = _User(user_id) if user_id is not None else None
        self.effective_chat = type("Chat", (), {"id": chat_id})()


async def _wait_until(predicate) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=1)


def test_process_update_override_keeps_ptb_async_signature():
    base_parameters = inspect.signature(BaseUpdateProcessor.process_update).parameters
    custom_parameters = inspect.signature(UserScopedUpdateProcessor.process_update).parameters

    assert [(name, parameter.kind) for name, parameter in custom_parameters.items()] == [
        (name, parameter.kind) for name, parameter in base_parameters.items()
    ]
    assert inspect.iscoroutinefunction(UserScopedUpdateProcessor.process_update)


@pytest.mark.asyncio
async def test_same_user_updates_are_serialized_across_chats():
    processor = UserScopedUpdateProcessor(50)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    async def first_handler() -> None:
        order.append("first-start")
        first_started.set()
        await release_first.wait()
        order.append("first-end")

    async def second_handler() -> None:
        order.append("second-start")
        second_started.set()

    first_task = asyncio.create_task(processor.process_update(_Update(7, chat_id=100), first_handler()))
    await asyncio.wait_for(first_started.wait(), timeout=1)

    second_task = asyncio.create_task(processor.process_update(_Update(7, chat_id=200), second_handler()))
    await _wait_until(lambda: processor._user_locks[7].references == 2)

    assert not second_started.is_set()
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first-start", "first-end", "second-start"]
    assert processor._user_locks == {}


@pytest.mark.asyncio
async def test_fifty_different_users_can_run_concurrently():
    processor = UserScopedUpdateProcessor(50)
    all_started = asyncio.Event()
    release = asyncio.Event()
    started: set[int] = set()

    async def handler(user_id: int) -> None:
        started.add(user_id)
        if len(started) == 50:
            all_started.set()
        await release.wait()

    tasks = [
        asyncio.create_task(processor.process_update(_Update(user_id), handler(user_id)))
        for user_id in range(50)
    ]

    await asyncio.wait_for(all_started.wait(), timeout=1)
    assert processor.current_concurrent_updates == 50
    release.set()
    await asyncio.gather(*tasks)

    assert processor.current_concurrent_updates == 0
    assert processor._user_locks == {}


@pytest.mark.asyncio
async def test_same_user_backlog_does_not_block_another_user():
    processor = UserScopedUpdateProcessor(50)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    release_backlog = asyncio.Event()
    other_user_started = asyncio.Event()

    async def first_handler() -> None:
        first_started.set()
        await release_first.wait()

    async def queued_handler() -> None:
        await release_backlog.wait()

    async def other_user_handler() -> None:
        other_user_started.set()

    first_task = asyncio.create_task(processor.process_update(_Update(7), first_handler()))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    backlog_tasks = [
        asyncio.create_task(processor.process_update(_Update(7), queued_handler())) for _ in range(50)
    ]
    await _wait_until(lambda: processor._user_locks[7].references >= 50)

    other_user_task = asyncio.create_task(processor.process_update(_Update(8), other_user_handler()))
    try:
        await asyncio.wait_for(other_user_started.wait(), timeout=1)
        started_without_waiting_for_user_7 = True
    except TimeoutError:
        started_without_waiting_for_user_7 = False
    finally:
        release_first.set()
        release_backlog.set()
        await asyncio.gather(first_task, *backlog_tasks, other_user_task)

    assert started_without_waiting_for_user_7
    assert processor._user_locks == {}


@pytest.mark.asyncio
async def test_update_without_effective_user_is_processed_without_a_user_lock():
    processor = UserScopedUpdateProcessor(50)
    processed = False

    async def handler() -> None:
        nonlocal processed
        processed = True

    await processor.process_update(_Update(None), handler())

    assert processed is True
    assert processor._user_locks == {}


@pytest.mark.asyncio
async def test_failed_update_releases_its_user_lock():
    processor = UserScopedUpdateProcessor(50)

    async def handler() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await processor.process_update(_Update(7), handler())

    assert processor.current_concurrent_updates == 0
    assert processor._user_locks == {}


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_its_user_lock_reference():
    processor = UserScopedUpdateProcessor(50)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_handler() -> None:
        first_started.set()
        await release_first.wait()

    first_task = asyncio.create_task(processor.process_update(_Update(7), first_handler()))
    await asyncio.wait_for(first_started.wait(), timeout=1)

    never_started = asyncio.get_running_loop().create_future()
    waiting_task = asyncio.create_task(processor.process_update(_Update(7), never_started))
    await _wait_until(lambda: processor._user_locks[7].references == 2)
    waiting_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task

    release_first.set()
    await first_task
    never_started.cancel()

    assert processor.current_concurrent_updates == 0
    assert processor._user_locks == {}


@pytest.mark.asyncio
async def test_cancelled_waiter_closes_its_unstarted_update_coroutine():
    processor = UserScopedUpdateProcessor(50)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_handler() -> None:
        first_started.set()
        await release_first.wait()

    async def queued_handler() -> None:
        raise AssertionError("cancelled queued update must never start")

    first_task = asyncio.create_task(processor.process_update(_Update(7), first_handler()))
    await asyncio.wait_for(first_started.wait(), timeout=1)

    queued_coroutine = queued_handler()
    waiting_task = asyncio.create_task(processor.process_update(_Update(7), queued_coroutine))
    await _wait_until(lambda: processor._user_locks[7].references == 2)
    waiting_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task

    try:
        assert inspect.getcoroutinestate(queued_coroutine) == inspect.CORO_CLOSED
    finally:
        queued_coroutine.close()
        release_first.set()
        await first_task


@pytest.mark.asyncio
async def test_cancelled_no_user_update_closes_coroutine_waiting_for_global_slot():
    processor = UserScopedUpdateProcessor(1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_handler() -> None:
        first_started.set()
        await release_first.wait()

    async def queued_handler() -> None:
        raise AssertionError("cancelled queued update must never start")

    first_task = asyncio.create_task(processor.process_update(_Update(7), first_handler()))
    await asyncio.wait_for(first_started.wait(), timeout=1)

    queued_coroutine = queued_handler()
    waiting_task = asyncio.create_task(processor.process_update(_Update(None), queued_coroutine))
    await asyncio.sleep(0)
    waiting_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task

    try:
        assert inspect.getcoroutinestate(queued_coroutine) == inspect.CORO_CLOSED
    finally:
        queued_coroutine.close()
        release_first.set()
        await first_task
