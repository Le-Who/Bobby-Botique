"""Database outages must not turn unknown state into a destructive reset."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app import state
from app.repos import users
from app.utils.background_tasks import TaskManager

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def state_store(monkeypatch):
    manager = TaskManager()
    monkeypatch.setattr(state, "USER_STATES", state._UserStateStore(maxsize=2))
    monkeypatch.setattr(state, "_pending_persists", {})
    monkeypatch.setattr("app.utils.background_tasks.try_submit_task", manager.try_submit)
    yield manager
    for timer in state._pending_persists.values():
        timer.cancel()
    await manager.drain(timeout=0)


@pytest.mark.parametrize("error_type", [asyncpg.InterfaceError, asyncpg.PostgresError])
async def test_failed_hydration_propagates_and_retries_without_losing_saved_fields(
    state_store, monkeypatch, error_type
):
    loads = 0

    async def query(sql, params):
        nonlocal loads
        assert "SELECT *" in sql
        assert params == (42,)
        loads += 1
        if loads == 1:
            raise error_type("synthetic unavailable state store")
        return [{"document_mode": True, "selected_document_id": 5, "role_diaries": {"role": ["saved note"]}}]

    monkeypatch.setattr(users, "db_query", query)
    with pytest.raises(error_type):
        await state.ensure_state_loaded(42)
    assert not state.get_user_state(42)._loaded_from_db

    recovered = await state.ensure_state_loaded(42)
    assert loads == 2
    assert recovered.document_mode
    assert recovered.selected_document_id == 5
    assert recovered.role_diaries == {"role": ["saved note"]}
    assert recovered._loaded_from_db


async def test_absent_state_is_loaded_once_without_retry(state_store, monkeypatch):
    loads = []

    async def query(sql, params):
        loads.append(params)
        return []

    monkeypatch.setattr(users, "db_query", query)
    await state.ensure_state_loaded(42)
    loaded = await state.ensure_state_loaded(42)
    assert loads == [(42,)]
    assert loaded._loaded_from_db
    assert not loaded.document_mode


async def test_cancelled_write_retains_marker_for_final_flush(state_store, monkeypatch):
    started = asyncio.Event()

    async def query(sql, params):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(users, "db_query", query)
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 60)
    current = state.get_user_state(42)
    current.selected_document_id = 5
    owner = asyncio.create_task(state._persist(current))
    await started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert 42 in state._pending_persists, "cancelled accepted write vanished before shutdown flush"


async def test_cancelled_queued_write_retains_latest_snapshot_for_final_flush(state_store, monkeypatch):
    import bot

    first_started = asyncio.Event()
    queued_started = asyncio.Event()
    release_first = asyncio.Event()
    writes = []

    async def query(sql, params):
        writes.append(params[2])
        if len(writes) == 1:
            first_started.set()
            await release_first.wait()
        return []

    async def noop(*args, **kwargs):
        return 0

    monkeypatch.setattr(users, "db_query", query)
    monkeypatch.setattr("app.repos.memory_autosave.drain_pending_memory_writes", noop)
    monkeypatch.setattr("app.repos.memory_autosave.pre_shutdown_compact", noop)
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 60)
    current = state.get_user_state(42)
    current.selected_document_id = 5
    first = asyncio.create_task(state._persist(current))
    await first_started.wait()
    current.selected_document_id = 6

    async def queued_write():
        queued_started.set()
        await state._persist(current)

    queued = asyncio.create_task(queued_write())
    try:
        await queued_started.wait()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert 42 in state._pending_persists, "write cancelled waiting for persistence lock lost its snapshot"
        release_first.set()
        await first
        await bot._flush_request_persistence()
        assert writes == [5, 6]
        assert not state._pending_persists
    finally:
        release_first.set()
        first.cancel()
        queued.cancel()
        await asyncio.gather(first, queued, return_exceptions=True)


async def test_failed_final_flush_does_not_rearm_tasks_or_report_success(state_store, monkeypatch, caplog):
    import bot

    async def query(sql, params):
        raise asyncpg.InterfaceError("synthetic final flush failure")

    async def noop(*args, **kwargs):
        return 0

    monkeypatch.setattr(users, "db_query", query)
    monkeypatch.setattr("app.repos.memory_autosave.drain_pending_memory_writes", noop)
    monkeypatch.setattr("app.repos.memory_autosave.pre_shutdown_compact", noop)
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 60)
    state.set_document_mode(42, True, document_id=5)
    await bot._flush_request_persistence()
    assert not state._pending_persists, "final flush rearmed work after the task drain"
    assert "Flushed 1 state persists" not in caplog.text
    assert "State drain failed" in caplog.text


@pytest.mark.parametrize("error_type", [asyncpg.InterfaceError, asyncpg.PostgresError])
async def test_failed_debounced_save_retries_latest_fields(state_store, monkeypatch, error_type):
    failed = asyncio.Event()
    saved = asyncio.Event()
    writes = []

    async def query(sql, params):
        assert "INSERT INTO user_state" in sql
        writes.append((params[0], params[2]))
        if len(writes) == 1:
            failed.set()
            raise error_type("synthetic failed state write")
        saved.set()
        return []

    monkeypatch.setattr(users, "db_query", query)
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 0.01)
    state.set_document_mode(42, True, document_id=5)
    await asyncio.wait_for(failed.wait(), 1)
    # The failed snapshot is superseded before the retained retry executes.
    state.get_user_state(42).selected_document_id = 6
    await asyncio.wait_for(saved.wait(), 1)
    assert await state_store.drain(timeout=1)
    assert writes == [(42, 5), (42, 6)]
    assert not state._pending_persists


@pytest.mark.parametrize(
    "entry",
    [
        "documents_command",
        "doc:cancel",
        "doc:use_existing:5",
        "doc:select:5",
        "doc:clear_all_confirm",
        "doc:delete_document:5",
        "role_create_callback",
        "role_create_cancel_callback",
        "role_custom_apply_callback",
        "role_custom_save_callback",
        "role_custom_retry_callback",
        "role_create_manual_callback",
        "role_manual_cancel_callback",
        "role_manual_save_callback",
    ],
)
async def test_cold_state_entry_preserves_unrelated_durable_fields(state_store, monkeypatch, entry):
    from app.handlers import cb_documents, cb_roles, commands

    saved_rows = []

    async def query(sql, params):
        if "SELECT *" in sql:
            return [
                {
                    "document_mode": True,
                    "selected_document_id": 5,
                    "role_diaries": {"role": ["saved note"]},
                    "tarot_mode": True,
                    "tarot_session": {"phase": "active"},
                    "generated_role": {"title": "Saved role", "prompt": "Saved prompt"},
                    "manual_role_title": "Manual role",
                    "manual_role_prompt": "Manual prompt",
                }
            ]
        saved_rows.append(params)
        return []

    monkeypatch.setattr(users, "db_query", query)
    monkeypatch.setattr(state, "_schedule_persist", lambda user_state: None)
    user = SimpleNamespace(id=42)
    message = SimpleNamespace(reply_text=AsyncMock())
    callback = SimpleNamespace(from_user=user, data=entry, answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(effective_user=user, message=message, callback_query=callback)
    context = SimpleNamespace(user_data={}, args=[])
    monkeypatch.setattr("app.handlers.menus.get_documents_menu_content", AsyncMock(return_value=("docs", None, None)))
    monkeypatch.setattr("app.handlers.menus.get_roles_menu_content", AsyncMock(return_value=("roles", None, None)))
    monkeypatch.setattr(cb_roles, "get_user_chat", AsyncMock(return_value=SimpleNamespace(system_prompt="")))
    monkeypatch.setattr(cb_roles, "update_user_chat", AsyncMock())
    monkeypatch.setattr(cb_roles, "create_custom_role", AsyncMock())
    monkeypatch.setattr(cb_roles.role_conv_metrics, "record_custom_role_creation", AsyncMock())
    document = {"id": 5, "filename": "test.pdf", "pages": 1, "created_at": "2026-10-25"}
    monkeypatch.setattr(cb_documents, "get_document_by_id", AsyncMock(return_value=document))
    monkeypatch.setattr(cb_documents, "get_user_documents", AsyncMock(return_value=[]))
    monkeypatch.setattr(cb_documents, "delete_all_user_documents", AsyncMock(return_value=1))
    monkeypatch.setattr(cb_documents, "delete_user_document", AsyncMock(return_value=True))

    if entry == "documents_command":
        await inspect.unwrap(commands.documents_command)(update, context)
    elif entry.startswith("doc:"):
        await cb_documents.document_callback(update, context)
    else:
        await getattr(cb_roles, entry)(update, context)

    loaded = state.get_user_state(42)
    assert loaded._loaded_from_db, "state mutation ran before restoring unrelated saved fields"
    await state._persist(loaded)
    assert len(saved_rows) == 1
    assert saved_rows[0][12:15] == ({"role": ["saved note"]}, True, {"phase": "active"})
