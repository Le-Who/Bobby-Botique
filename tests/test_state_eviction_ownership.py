"""LRU eviction must preserve a still-live background/lock owner (ST-06)."""

import asyncio
import gc
import weakref

import pytest

from app import state

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(monkeypatch):
    store = state._UserStateStore(maxsize=1)
    monkeypatch.setattr(state, "USER_STATES", store)
    monkeypatch.setattr(state, "_pending_persists", {})
    yield store
    for handle in state._pending_persists.values():
        handle.cancel()


async def test_evicted_live_reference_keeps_lock_and_loaded_durable_fields(store):
    old = store[11]
    old._loaded_from_db = True
    old.document_mode = True
    old.selected_document_id = 42
    old._dirty = True
    async with old.lock:
        store[12]
        assert state.get_active_user_lock_count() == 1
        current = store[11]
        assert current.lock is old.lock, "background references must not acquire a second user lock after eviction"
        assert current.document_mode and current.selected_document_id == 42
        assert current._loaded_from_db and current._dirty
        assert len(store._states) == 1
        assert state.get_active_user_lock_count() == 1


async def test_pending_persist_reuses_live_state_and_saves_latest_fields(store, monkeypatch):
    written = []
    saved = asyncio.Event()

    async def save(**data):
        written.append((data["user_id"], data["selected_document_id"], data["awaiting_manual_role_title"]))
        saved.set()

    monkeypatch.setattr("app.repos.users.save_user_state", save)
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 0)
    state.set_document_mode(11, True, document_id=41)
    state.begin_manual_role_creation(11)
    pending = state._pending_persists[11]
    store[12]
    state.set_document_mode(11, True, document_id=42)
    assert pending.cancelled()
    await asyncio.wait_for(saved.wait(), 1)
    assert written == [(11, 42, True)]
    assert store[11].document_mode
    assert not state._pending_persists


async def test_unreferenced_evicted_state_can_be_collected(store):
    old = store[11]
    ref = weakref.ref(old)
    store[12]
    del old
    gc.collect()
    assert ref() is None, "live reference protection must not turn LRU into an unbounded strong-reference map"


async def test_lock_only_owner_preserves_state_across_eviction(store):
    lock = state.get_user_lock(11)
    async with lock:
        store[12]
        gc.collect()
        assert state.get_user_lock(11) is lock
        assert state.get_user_lock(11).locked()


async def test_purge_invalidates_evicted_live_state_and_stale_persist(store, monkeypatch):
    old = store[11]
    old.document_mode = True
    store[12]
    written = []

    async def save(**data):
        written.append(data)

    monkeypatch.setattr("app.repos.users.save_user_state", save)
    state.purge_user_runtime_state(11)
    state._schedule_persist(old)
    await state._persist(old)
    assert written == [], "a pre-erasure reference may not restore erased user data"
    assert not state._pending_persists
    replacement = store[11]
    assert replacement is not old
    assert not replacement.document_mode
