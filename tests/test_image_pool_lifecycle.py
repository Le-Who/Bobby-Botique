"""Image worker lifetime and output contracts; synthetic inputs, no providers."""

import asyncio
import concurrent.futures
import io
import os
import threading
import time
from concurrent.futures.process import BrokenProcessPool

import pytest
from PIL import Image

from app.utils import image_utils


class ManualTimer:
    created = []

    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args
        self.kwargs = kwargs or {}
        self.cancelled = False
        self.daemon = False
        self.created.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A timer callback can already be running when cancellation happens.
        self.function(*self.args, **self.kwargs)


class ControlledPool:
    def __init__(self, *futures, submit_error=None):
        self.futures = iter(futures)
        self.submit_error = submit_error
        self.calls = []
        self.shutdowns = []

    def submit(self, function, *args):
        self.calls.append((function, args))
        if self.submit_error:
            raise self.submit_error
        return next(self.futures)

    def shutdown(self, **kwargs):
        self.shutdowns.append(kwargs)


def completed(value):
    future = concurrent.futures.Future()
    future.set_result(value)
    return future


@pytest.fixture
def timers(monkeypatch):
    ManualTimer.created = []
    monkeypatch.setattr(threading, "Timer", ManualTimer)
    return ManualTimer.created


@pytest.fixture(autouse=True)
def clean_image_pool():
    image_utils.shutdown_image_pool()
    image_utils.clear_image_cache()
    image_utils._image_process_pool_failed = False
    yield
    image_utils.shutdown_image_pool()
    image_utils.clear_image_cache()
    image_utils._image_process_pool_failed = False


@pytest.mark.asyncio
async def test_process_pool_reuses_at_most_two_workers_across_sequential_jobs():
    pool = image_utils._get_image_process_pool()
    assert pool is not None
    worker_ids = []
    try:
        for _ in range(6):
            worker_ids.append(await asyncio.wrap_future(pool.submit(os.getpid)))
        assert os.getpid() not in worker_ids
        assert len(set(worker_ids)) <= 2
    finally:
        image_utils.shutdown_image_pool()
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_idle_pool_is_released_and_next_request_creates_a_new_pool(monkeypatch, timers):
    first_pool = ControlledPool(completed(b"first"))
    second_pool = ControlledPool(completed(b"second"))
    monkeypatch.setattr(image_utils, "_image_process_pool", first_pool)
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", lambda **_kwargs: second_pool)
    assert await image_utils.save_image_as_bytes(b"input") == b"first"
    assert len(timers) == 1 and timers[0].interval == 60 and timers[0].daemon
    timers[0].fire()
    assert first_pool.shutdowns == [{"wait": False, "cancel_futures": True}]
    assert image_utils._image_process_pool is None
    assert await image_utils.save_image_as_bytes(b"input") == b"second"


@pytest.mark.asyncio
async def test_cancelled_idle_timer_cannot_retire_a_busy_or_reused_pool(monkeypatch, timers):
    running = concurrent.futures.Future()
    running.set_running_or_notify_cancel()
    pool = ControlledPool(completed(b"first"), running)
    monkeypatch.setattr(image_utils, "_image_process_pool", pool)
    assert await image_utils.save_image_as_bytes(b"input") == b"first"
    old_timer = timers[0]
    task = asyncio.create_task(image_utils.save_image_as_bytes(b"next"))
    await asyncio.sleep(0)
    assert old_timer.cancelled
    old_timer.fire()
    assert pool.shutdowns == []
    running.set_result(b"second")
    assert await task == b"second"
    old_timer.fire()
    assert pool.shutdowns == []
    timers[-1].fire()
    assert len(pool.shutdowns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["timeout", "cancel"])
async def test_abandoned_await_keeps_the_worker_busy_until_its_real_completion(monkeypatch, timers, exit_kind):
    running = concurrent.futures.Future()
    running.set_running_or_notify_cancel()
    pool = ControlledPool(running)
    monkeypatch.setattr(image_utils, "_image_process_pool", pool)
    task = asyncio.create_task(image_utils.save_image_as_bytes(b"input", timeout=0.01 if exit_kind == "timeout" else 5))
    await asyncio.sleep(0)
    if exit_kind == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task is None
    assert timers == []
    assert pool.shutdowns == []
    running.set_result(b"completed after caller left")
    assert len(timers) == 1
    timers[0].fire()
    assert len(pool.shutdowns) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["submit", "result"])
async def test_broken_pool_is_retired_and_the_next_request_recovers(monkeypatch, timers, failure_at):
    failed = concurrent.futures.Future()
    failed.set_exception(BrokenProcessPool("synthetic worker exit"))
    pool = ControlledPool(
        failed, submit_error=BrokenProcessPool("synthetic submit failure") if failure_at == "submit" else None
    )
    replacement = ControlledPool(completed(b"recovered"))
    monkeypatch.setattr(image_utils, "_image_process_pool", pool)
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", lambda **_kwargs: replacement)
    assert await image_utils.save_image_as_bytes(b"input") is None
    assert image_utils._image_process_pool is None
    assert len(pool.shutdowns) == (1 if failure_at == "submit" else 0)
    assert await image_utils.save_image_as_bytes(b"next") == b"recovered"


@pytest.mark.asyncio
async def test_shutdown_invalidates_idle_timer_and_old_completion(monkeypatch, timers):
    running = concurrent.futures.Future()
    running.set_running_or_notify_cancel()
    old_pool = ControlledPool(completed(b"first"), running)
    new_pool = ControlledPool(completed(b"new"))
    monkeypatch.setattr(image_utils, "_image_process_pool", old_pool)
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", lambda **_kwargs: new_pool)
    await image_utils.save_image_as_bytes(b"input")
    old_timer = timers[0]
    task = asyncio.create_task(image_utils.save_image_as_bytes(b"old pending"))
    await asyncio.sleep(0)
    image_utils.shutdown_image_pool()
    assert await image_utils.save_image_as_bytes(b"new input") == b"new"
    running.set_result(b"old result")
    assert await task == b"old result"
    old_timer.fire()
    assert image_utils._image_process_pool is new_pool
    assert new_pool.shutdowns == []


@pytest.mark.asyncio
async def test_real_pool_preserves_outputs_errors_cache_and_caller_image():
    for mode in ("RGB", "RGBA", "P", "L", "CMYK"):
        with Image.new(mode, (1800, 96)) as image:
            for task_type in ("default", "describe", "search", "ocr", "unknown"):
                expected = image_utils._image_worker(image.copy(), task_type=task_type)
                actual = await image_utils.save_image_as_bytes(image, task_type=task_type)
                assert actual == expected
                assert image.size == (1800, 96)
    with Image.new("RGB", (32, 16), (20, 40, 60)) as image:
        buf = io.BytesIO()
        image.save(buf, "PNG")
    raw = buf.getvalue()
    expected = image_utils._image_worker(raw)
    assert expected is not None
    assert await image_utils.save_image_as_bytes(raw, cache_key="synthetic-image") == expected
    # Cache hits retain the previous behavior, bypassing even invalid input.
    assert await image_utils.save_image_as_bytes(b"invalid", cache_key="synthetic-image") == expected
    assert await image_utils.save_image_as_bytes(b"invalid") is None
    assert await image_utils.save_image_as_bytes(raw) == expected


@pytest.mark.asyncio
async def test_real_idle_expiry_stops_workers_and_request_restarts_them(monkeypatch):
    monkeypatch.setattr(image_utils, "_IMAGE_POOL_IDLE_SECONDS", 0.1)
    pool = image_utils._get_image_process_pool()
    assert pool is not None
    await asyncio.wrap_future(pool.submit(os.getpid))
    processes = list(pool._processes.values())
    with Image.new("RGB", (32, 16)) as image:
        assert await image_utils.save_image_as_bytes(image) is not None
    async with asyncio.timeout(10):
        while image_utils._image_process_pool is pool or any(p.is_alive() for p in processes):
            await asyncio.sleep(0.02)
    for process in processes:
        process.join(timeout=0)
        assert process.exitcode == 0
    with Image.new("RGB", (32, 16)) as image:
        assert await image_utils.save_image_as_bytes(image) is not None


@pytest.mark.asyncio
async def test_real_worker_crash_does_not_poison_next_request():
    pool = image_utils._get_image_process_pool()
    assert pool is not None
    with pytest.raises(BrokenProcessPool):
        await asyncio.wrap_future(pool.submit(os._exit, 17))
    # The crashed pool may first be detected by submit or by its result callback.
    with Image.new("RGB", (32, 16)) as image:
        assert await image_utils.save_image_as_bytes(image) is None
        assert await image_utils.save_image_as_bytes(image) is not None


@pytest.mark.asyncio
async def test_real_worker_crash_completes_pending_managed_request_and_cleans_up(monkeypatch):
    factory = concurrent.futures.ProcessPoolExecutor
    monkeypatch.setattr(
        concurrent.futures,
        "ProcessPoolExecutor",
        lambda **kwargs: factory(**{**kwargs, "max_workers": 1}),
    )
    pool = image_utils._get_image_process_pool()
    # Occupy the sole worker so the managed image request cannot finish first.
    sleeper = pool.submit(time.sleep, 10)
    manager = pool._executor_manager_thread
    processes = list(pool._processes.values())
    with Image.new("RGB", (32, 16)) as image:
        request = asyncio.create_task(image_utils.save_image_as_bytes(image, timeout=10))
        await asyncio.sleep(0)
        assert image_utils._image_pool_jobs == 1
        for process in processes:
            process.terminate()
        assert await asyncio.wait_for(request, 3) is None
        with pytest.raises(BrokenProcessPool):
            await asyncio.wrap_future(sleeper)
        await asyncio.to_thread(manager.join, 3)
        assert not manager.is_alive()
        assert all(not process.is_alive() for process in processes)
        assert image_utils._image_process_pool is None
        assert await image_utils.save_image_as_bytes(image) is not None


@pytest.mark.asyncio
async def test_submit_does_not_hold_lifecycle_lock(monkeypatch, timers):
    class LockCheckingPool(ControlledPool):
        def submit(self, function, *args):
            acquired = threading.Event()

            def callback_thread():
                with image_utils._image_pool_lock:
                    acquired.set()

            thread = threading.Thread(target=callback_thread, daemon=True)
            thread.start()
            assert acquired.wait(1), "submit holds the lifecycle lock and can deadlock a completion callback"
            thread.join()
            return super().submit(function, *args)

    pool = LockCheckingPool(completed(b"ok"))
    monkeypatch.setattr(image_utils, "_image_process_pool", pool)
    assert await image_utils.save_image_as_bytes(b"input") == b"ok"


@pytest.mark.asyncio
async def test_unavailable_idle_timer_releases_pool_without_losing_result(monkeypatch, timers):
    def fail_start(self):
        raise RuntimeError("synthetic thread limit")

    monkeypatch.setattr(ManualTimer, "start", fail_start)
    pool = ControlledPool(completed(b"compressed"))
    monkeypatch.setattr(image_utils, "_image_process_pool", pool)
    assert await image_utils.save_image_as_bytes(b"input") == b"compressed"
    assert image_utils._image_process_pool is None
    assert len(pool.shutdowns) == 1
