import asyncio
import logging
import threading

import pytest

from app.request_context import (
    clear_request_id,
    clear_user_context,
    get_chat_id,
    get_request_id,
    get_user_id,
    set_request_id,
    set_user_context,
)
from app.utils.logging_config import RequestContextFilter


@pytest.fixture(autouse=True)
def _clear_context_between_tests():
    clear_request_id()
    clear_user_context()
    yield
    clear_request_id()
    clear_user_context()


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


# ── user context tests ──────────────────────────────────────────────────


def test_set_user_context_stores_values():
    set_user_context(user_id=42, chat_id=100)

    assert get_user_id() == 42
    assert get_chat_id() == 100


def test_clear_user_context_resets_values():
    set_user_context(user_id=42, chat_id=100)
    clear_user_context()

    assert get_user_id() is None
    assert get_chat_id() is None


def test_user_context_defaults_to_none():
    assert get_user_id() is None
    assert get_chat_id() is None


@pytest.mark.asyncio
async def test_user_context_isolated_between_coroutines():
    async def worker(uid: int, cid: int):
        set_user_context(user_id=uid, chat_id=cid)
        await asyncio.sleep(0)
        return get_user_id(), get_chat_id()

    (uid_a, cid_a), (uid_b, cid_b) = await asyncio.gather(
        worker(1, 10), worker(2, 20)
    )

    assert uid_a == 1
    assert cid_a == 10
    assert uid_b == 2
    assert cid_b == 20


@pytest.mark.asyncio
async def test_create_task_inherits_user_context():
    async def child():
        await asyncio.sleep(0)
        return get_user_id(), get_chat_id()

    set_user_context(user_id=99, chat_id=999)
    task = asyncio.create_task(child())
    set_user_context(user_id=0, chat_id=0)

    child_uid, child_cid = await task

    assert child_uid == 99
    assert child_cid == 999
    assert get_user_id() == 0


def test_context_filter_injects_user_and_chat_id():
    filter_ = RequestContextFilter()
    set_request_id("rid-ctx")
    set_user_context(user_id=42, chat_id=100)

    record = logging.LogRecord(
        name="test-logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    filter_.filter(record)

    assert record.request_id == "rid-ctx"
    assert record.user_id == 42
    assert record.chat_id == 100


def test_context_filter_injects_none_when_no_context():
    filter_ = RequestContextFilter()
    record = logging.LogRecord(
        name="test-logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    filter_.filter(record)

    assert record.user_id is None
    assert record.chat_id is None
