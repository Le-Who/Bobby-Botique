"""Chat history checkpoints follow acknowledged repository transactions."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from app import database
from app.database import ChatState
from app.repos import chats


class ChatDriver:
    """Offline transaction boundary with staged message writes."""

    _closed = False

    def __init__(self):
        self.epoch = 41
        self.messages = []
        self.query_error = None
        self.commit_error = None
        self.accept = True
        self.pause_commit = False
        self.commit_started = asyncio.Event()
        self.release_commit = asyncio.Event()
        self.pending = None

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        try:
            yield
            self.commit_started.set()
            if self.pause_commit:
                await self.release_commit.wait()
            if self.commit_error is not None:
                raise self.commit_error
            if self.pending is not None:
                delete, messages = self.pending
                if delete:
                    self.messages.clear()
                self.messages.extend(messages)
        finally:
            self.pending = None

    async def fetch(self, sql, *params):
        assert "WITH update_chats AS" in sql
        if self.query_error is not None:
            raise self.query_error
        if not self.accept or params[20] not in (None, self.epoch):
            return []
        self.pending = (params[10], list(zip(params[11], params[12], strict=True)))
        return [{"memory_epoch": self.epoch}]


@pytest.fixture
def driver(monkeypatch):
    driver = ChatDriver()
    monkeypatch.setattr(database.db_manager, "pool", driver)
    monkeypatch.setattr(chats, "set_user_context", AsyncMock())
    monkeypatch.setattr(chats, "clear_user_context", AsyncMock())
    return driver


def new_chat():
    return ChatState(
        history=[{"role": "user", "content": "Synthetic pending message"}],
        model="gemini-3.1-flash-lite",
        token_count=0,
        search_enabled=False,
        system_prompt=None,
        _has_persisted_chat=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["query", "commit", "cancel", "rejected"])
async def test_failed_chat_save_retains_unpersisted_history_for_confirmed_retry(driver, failure):
    chat = new_chat()
    if failure == "query":
        driver.query_error = asyncpg.SerializationError("synthetic aborted statement")
    elif failure == "commit":
        driver.commit_error = asyncpg.SerializationError("synthetic aborted commit")
    elif failure == "cancel":
        driver.commit_error = asyncio.CancelledError()
    else:
        driver.accept = False

    if failure == "rejected":
        assert await chats.update_user_chat(42, chat) is False
    else:
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else asyncpg.SerializationError):
            await chats.update_user_chat(42, chat)
    assert driver.messages == []

    driver.query_error = None
    driver.commit_error = None
    driver.accept = True
    assert await chats.update_user_chat(42, chat) is True
    assert driver.messages == [("user", "Synthetic pending message")]


@pytest.mark.asyncio
async def test_chat_generation_and_history_checkpoint_wait_for_commit_acknowledgment(driver):
    chat = new_chat()
    driver.pause_commit = True
    saving = asyncio.create_task(chats.update_user_chat(42, chat))
    try:
        await asyncio.wait_for(driver.commit_started.wait(), 1)
        assert chat.memory_epoch == 0
        assert chat._has_persisted_chat is False
        assert chat._original_length == 0
        driver.release_commit.set()
        assert await saving is True
        assert chat.memory_epoch == 41
        assert chat._has_persisted_chat is True
        assert chat._original_length == 1
        assert await chats.update_user_chat(42, chat) is True
        assert driver.messages == [("user", "Synthetic pending message")]
    finally:
        driver.release_commit.set()
        saving.cancel()
        await asyncio.gather(saving, return_exceptions=True)


@pytest.mark.asyncio
async def test_unacknowledged_commit_does_not_publish_a_successful_generation(driver):
    chat = new_chat()
    driver.commit_error = asyncpg.InterfaceError("synthetic unknown commit acknowledgment")

    with pytest.raises(asyncpg.InterfaceError):
        await chats.update_user_chat(42, chat)

    # An unknown commit outcome requires a durable reload by the caller, not
    # an automatic append retry. The local object must not claim success.
    assert chat.memory_epoch == 0
    assert chat._has_persisted_chat is False
    assert chat._original_length == 0


@pytest.mark.asyncio
async def test_message_appended_during_commit_remains_pending_for_the_next_save(driver):
    chat = new_chat()
    driver.pause_commit = True
    saving = asyncio.create_task(chats.update_user_chat(42, chat))
    try:
        await asyncio.wait_for(driver.commit_started.wait(), 1)
        chat.history.append({"role": "model", "content": "Synthetic later message"})
        driver.release_commit.set()
        assert await saving is True
        assert chat._original_length == 1
        assert driver.messages == [("user", "Synthetic pending message")]

        assert await chats.update_user_chat(42, chat) is True
        assert chat._original_length == 2
        assert driver.messages == [
            ("user", "Synthetic pending message"),
            ("model", "Synthetic later message"),
        ]
    finally:
        driver.release_commit.set()
        saving.cancel()
        await asyncio.gather(saving, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_generation_remains_rejected_without_advancing_history(driver):
    chat = new_chat()
    chat.memory_epoch = 40
    chat._has_persisted_chat = True

    assert await chats.update_user_chat(42, chat) is False

    assert driver.messages == []
    assert chat.memory_epoch == 40
    assert chat._has_persisted_chat is True
    assert chat._original_length == 0


def test_chat_state_declares_persisted_chat_marker() -> None:
    field = ChatState.__dataclass_fields__["_has_persisted_chat"]
    assert field.default is True


def _pool_boundary():
    conn = MagicMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = transaction

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire
    return conn, SimpleNamespace(pool=pool, is_connected=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dropped_count", "retained_roles"),
    [
        (1, ["model", "user", "model"]),
        (2, ["user", "model"]),
    ],
)
async def test_compacted_history_forces_full_rewrite_regardless_of_final_length(
    dropped_count,
    retained_roles,
):
    """Dropping 1/2 turns cannot be mistaken for append-only/equal length."""
    history = [
        {"role": role, "parts": [f"content-{index}"]} for index, role in enumerate([*retained_roles, "user", "model"])
    ]
    state = ChatState(
        history=history,
        model="gemini-3.1-flash-lite",
        token_count=10,
        search_enabled=False,
        system_prompt=None,
        _original_length=4,
    )
    conn, manager = _pool_boundary()

    with (
        patch.object(chats, "db_manager", manager),
        patch.object(chats, "db_query", new_callable=AsyncMock) as query,
        patch.object(chats, "set_user_context", new_callable=AsyncMock),
        patch.object(chats, "clear_user_context", new_callable=AsyncMock),
    ):
        await chats.update_user_chat(42, state, rewrite_history=True)

    params = query.await_args.args[1]
    assert params[10] is True
    assert params[11] == [message["role"] for message in history]
    assert params[12] == [message["parts"][0] for message in history]
    assert state._original_length == 6 - dropped_count
    assert query.await_args.kwargs["conn"] is conn
