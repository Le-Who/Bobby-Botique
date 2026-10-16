"""Bulk deletion distinguishes driver failures from empty and successful results.

Keep the manager/query, repository, processor facade, callback, menu and state
real. Only the external driver, reconnect, Telegram and persistence edges are
synthetic. Cancellation is tested before fake mutation, not after a real commit.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import asyncpg
import pytest

from app import database, state
from app.document_processor import delete_all_user_documents
from app.handlers import cb_documents
from app.i18n import t

DELETE_SQL = "DELETE FROM user_documents WHERE user_id = $1 RETURNING id"


class SyntheticPool:
    """A bounded fake driver; no database transaction/commit semantics claimed."""

    _closed = False

    def __init__(self):
        self.rows = {(101, 11): self.row(11, "synthetic.pdf"), (202, 22): self.row(22, "other.pdf")}
        self.error = None
        self.failures_remaining = 0
        self.pause_before_mutation = False
        self.entered = asyncio.Event()
        self.release_mutation = asyncio.Event()
        self.delete_calls = []
        self.list_calls = []
        self.context_calls = []
        self.acquired = 0
        self.released = 0
        self.transactions_entered = 0
        self.transactions_exited = 0

    @staticmethod
    def row(doc_id, filename):
        return {
            "id": doc_id,
            "filename": filename,
            "pages": 1,
            "created_at": datetime(2026, 1, 1),
            "file_size": 12,
            "file_hash": "fake",
        }

    @asynccontextmanager
    async def acquire(self):
        self.acquired += 1
        try:
            yield self
        finally:
            self.released += 1

    @asynccontextmanager
    async def transaction(self):
        self.transactions_entered += 1
        try:
            yield self
        finally:
            self.transactions_exited += 1

    async def fetch(self, query, *params):
        if query.lstrip().startswith("DELETE"):
            self.delete_calls.append((query, params))
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise self.error
            if self.pause_before_mutation:
                self.entered.set()
                await self.release_mutation.wait()
            keys = [key for key in self.rows if key[0] == params[0]]
            for key in keys:
                self.rows.pop(key)
            return [{"id": key[1]} for key in keys]
        if "set_config" in query:
            self.context_calls.append(params)
            return []
        if "FROM user_documents WHERE user_id = $1 ORDER BY created_at DESC" in query:
            self.list_calls.append(params)
            return [row for (owner, _), row in self.rows.items() if owner == params[0]]
        raise AssertionError("Unexpected SQL at synthetic document driver boundary")


def document_state(chat):
    return chat.document_mode, chat.selected_document_id, chat.last_document_message_id


def state_snapshot(chat):
    return deepcopy({name: getattr(chat, name) for name in chat.__slots__ if name not in {"__weakref__", "lock"}})


@pytest.fixture
def documents(monkeypatch):
    pool = SyntheticPool()
    monkeypatch.setattr(database.db_manager, "pool", pool)
    reconnect = AsyncMock()
    monkeypatch.setattr(database.db_manager, "reconnect", reconnect)
    monkeypatch.setattr(state, "_schedule_persist", lambda chat: None)
    states = {uid: state.get_user_state(uid) for uid in (101, 202)}
    for uid, doc_id in ((101, 11), (202, 22)):
        state.set_document_mode(uid, True, doc_id)
        states[uid].last_document_message_id = 77
    query = SimpleNamespace(
        data="doc:clear_all_confirm",
        from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    harness = SimpleNamespace(
        pool=pool,
        reconnect=reconnect,
        states=states,
        other_row=pool.rows[(202, 22)].copy(),
        other_state=state_snapshot(states[202]),
        query=query,
        update=SimpleNamespace(callback_query=query),
        context=SimpleNamespace(user_data={}),
    )
    yield harness
    assert pool.acquired == pool.released
    assert pool.transactions_entered == pool.transactions_exited
    assert pool.rows[(202, 22)] == harness.other_row
    assert state_snapshot(states[202]) == harness.other_state


def assert_attempts(documents, attempts, reconnects=0):
    assert documents.pool.delete_calls == [(DELETE_SQL, (101,))] * attempts
    assert documents.reconnect.await_count == reconnects
    assert documents.pool.acquired == documents.pool.released


def assert_original_selection(documents):
    assert document_state(documents.states[101]) == (True, 11, 77)
    assert document_state(documents.states[202]) == (True, 22, 77)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncpg.PostgresError, asyncpg.InterfaceError])
async def test_exhausted_driver_error_reaches_facade_unchanged(documents, caplog, error_type):
    """Returning zero or replacing the original exhausted error breaks callers."""
    error = error_type("synthetic deletion failure")
    documents.pool.error = error
    documents.pool.failures_remaining = 4
    before = documents.pool.rows.copy()

    with pytest.raises(error_type) as caught:
        await delete_all_user_documents(101)

    assert caught.value is error
    records = [record for record in caplog.records if record.name == "app.documents.repository"]
    assert len(records) == 1
    assert records[0].msg == "Error deleting all documents: %s"
    assert records[0].exc_info[1] is error
    assert documents.pool.rows == before
    assert_original_selection(documents)
    assert_attempts(documents, 4, 3 if error_type is asyncpg.InterfaceError else 0)
    assert documents.pool.list_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncpg.PostgresError, asyncpg.InterfaceError])
async def test_exhausted_driver_error_uses_controlled_callback_feedback(documents, error_type):
    """Swallowing driver failure produces false empty feedback and menu refresh."""
    documents.pool.error = error_type("synthetic deletion failure")
    documents.pool.failures_remaining = 4
    before = documents.pool.rows.copy()

    await cb_documents.document_callback(documents.update, documents.context)

    assert documents.query.answer.await_args_list == [call(), call(t("doc.delete_error"))]
    documents.query.edit_message_text.assert_not_awaited()
    assert documents.pool.list_calls == []
    assert documents.pool.rows == before
    assert_original_selection(documents)
    assert_attempts(documents, 4, 3 if error_type is asyncpg.InterfaceError else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 3])
async def test_facade_returns_exact_scoped_count_then_zero_on_replay(documents, count):
    """The real facade must retain integer counts and tenant-scoped mutation."""
    documents.pool.rows = {(202, 22): documents.other_row.copy()}
    for doc_id in range(11, 11 + count):
        documents.pool.rows[(101, doc_id)] = documents.pool.row(doc_id, f"synthetic-{doc_id}.pdf")

    result = await delete_all_user_documents(101)
    replay = await delete_all_user_documents(101)

    assert type(result) is int
    assert result == count
    assert type(replay) is int
    assert replay == 0
    assert documents.pool.rows == {(202, 22): documents.other_row}
    assert_original_selection(documents)
    assert_attempts(documents, 2)


@pytest.mark.asyncio
async def test_successful_callback_cleans_selection_and_replay_has_no_second_success(documents):
    """Missing cleanup, leaking another tenant or repeat positive feedback fails."""
    documents.pool.rows[(101, 12)] = documents.pool.row(12, "second.pdf")

    await cb_documents.document_callback(documents.update, documents.context)

    assert documents.query.answer.await_args_list == [call(), call("🗑️ Удалено 2 документов.")]
    assert document_state(documents.states[101]) == (False, None, None)
    assert documents.pool.rows == {(202, 22): documents.other_row}
    assert documents.pool.list_calls == [(101,)]
    documents.query.edit_message_text.assert_awaited_once()
    assert "other.pdf" not in documents.query.edit_message_text.await_args.args[0]
    assert documents.pool.context_calls == [("101", "false"), ()]

    await cb_documents.document_callback(documents.update, documents.context)

    assert documents.query.answer.await_args_list == [
        call(),
        call("🗑️ Удалено 2 документов."),
        call(),
        call(t("doc.no_documents_to_delete")),
    ]
    assert document_state(documents.states[101]) == (False, None, None)
    assert documents.query.edit_message_text.await_count == 2
    assert documents.pool.list_calls == [(101,), (101,)]
    assert_attempts(documents, 2)


@pytest.mark.asyncio
async def test_genuine_empty_callback_keeps_stale_selection_and_refreshes_real_menu(documents):
    """A genuinely empty deletion remains a normal empty outcome."""
    documents.pool.rows.pop((101, 11))

    await cb_documents.document_callback(documents.update, documents.context)

    assert documents.query.answer.await_args_list == [call(), call(t("doc.no_documents_to_delete"))]
    documents.query.edit_message_text.assert_awaited_once()
    assert "other.pdf" not in documents.query.edit_message_text.await_args.args[0]
    assert documents.pool.list_calls == [(101,)]
    assert_original_selection(documents)
    assert_attempts(documents, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncpg.PostgresError, asyncpg.InterfaceError])
async def test_transient_driver_failure_recovers_before_successful_callback(documents, error_type):
    """Propagation must not bypass the manager's existing retry/reconnect policy."""
    documents.pool.error = error_type("synthetic transient failure")
    documents.pool.failures_remaining = 1

    await cb_documents.document_callback(documents.update, documents.context)

    assert documents.query.answer.await_args_list == [call(), call("🗑️ Удалено 1 документов.")]
    assert document_state(documents.states[101]) == (False, None, None)
    assert documents.pool.rows == {(202, 22): documents.other_row}
    documents.query.edit_message_text.assert_awaited_once()
    assert_attempts(documents, 2, 1 if error_type is asyncpg.InterfaceError else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["facade", "callback"])
async def test_cancellation_before_driver_mutation_propagates_and_releases(documents, boundary):
    """A cancelled pending driver operation must not turn into terminal feedback."""
    documents.pool.pause_before_mutation = True
    before = documents.pool.rows.copy()
    operation = (
        delete_all_user_documents(101)
        if boundary == "facade"
        else cb_documents.document_callback(documents.update, documents.context)
    )
    task = asyncio.create_task(operation)
    try:
        await documents.pool.entered.wait()
        assert documents.pool.acquired == 1
        assert documents.pool.released == 0
        assert documents.pool.rows == before
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert documents.query.answer.await_args_list == ([call()] if boundary == "callback" else [])
    documents.query.edit_message_text.assert_not_awaited()
    assert documents.pool.rows == before
    assert documents.pool.list_calls == []
    assert_original_selection(documents)
    assert_attempts(documents, 1)
