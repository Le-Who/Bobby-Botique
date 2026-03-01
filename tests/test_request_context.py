import asyncio
import logging
import threading

import pytest

from app.request_context import clear_request_id, get_request_id, set_request_id
from app.utils.logging_config import RequestContextFilter


@pytest.fixture(autouse=True)
def _clear_context_between_tests():
    clear_request_id()
    yield
    clear_request_id()


def test_set_request_id_sets_explicit_value():
    request_id = set_request_id("explicit-id")

    assert request_id == "explicit-id"
    assert get_request_id() == "explicit-id"


def test_set_request_id_generates_id_when_missing():
    request_id = set_request_id()

    assert isinstance(request_id, str)
    assert len(request_id) == 12
    assert get_request_id() == request_id


def test_clear_request_id_resets_context_value():
    set_request_id("to-clear")

    clear_request_id()

    assert get_request_id() is None


@pytest.mark.asyncio
async def test_async_context_is_isolated_between_concurrent_coroutines():
    async def worker(value: str):
        set_request_id(value)
        await asyncio.sleep(0)
        return get_request_id()

    result_a, result_b = await asyncio.gather(worker("rid-a"), worker("rid-b"))

    assert result_a == "rid-a"
    assert result_b == "rid-b"


@pytest.mark.asyncio
async def test_create_task_inherits_parent_context_on_creation():
    async def child():
        await asyncio.sleep(0)
        return get_request_id()

    set_request_id("parent-before-task")
    task = asyncio.create_task(child())
    set_request_id("parent-after-task")

    child_request_id = await task

    assert child_request_id == "parent-before-task"
    assert get_request_id() == "parent-after-task"


def test_request_id_is_not_inherited_by_new_thread():
    set_request_id("main-thread-id")
    result = {}

    def worker():
        result["request_id"] = get_request_id()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert get_request_id() == "main-thread-id"
    assert result["request_id"] is None


def test_request_context_filter_injects_request_id_into_log_record():
    filter_ = RequestContextFilter()
    set_request_id("log-rid")
    record = logging.LogRecord(
        name="test-logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    accepted = filter_.filter(record)

    assert accepted is True
    assert record.request_id == "log-rid"
