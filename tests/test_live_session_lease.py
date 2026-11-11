"""Live admission keeps ownership until bounded session work and cleanup finish."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _LeaseStore:
    def __init__(self):
        self.now = 0
        self.values = {}
        self.release_started = asyncio.Event()
        self.release_allowed = asyncio.Event()
        self.release_allowed.set()

    async def set(self, key, value, *, nx=False, ex=None):
        previous = self.values.get(key)
        if previous and previous[1] <= self.now:
            del self.values[key]
        if nx and key in self.values:
            return False
        self.values[key] = (value, self.now + ex)
        return True

    async def delete(self, key):
        return self.values.pop(key, None) is not None

    async def eval(self, script, count, key, owner):
        self.release_started.set()
        await self.release_allowed.wait()
        if self.values.get(key, (None,))[0] == owner:
            del self.values[key]
            return 1
        return 0


class _Socket:
    args = {"initData": "synthetic"}

    def __init__(self):
        self.closed = []

    async def close(self, code, reason):
        self.closed.append((code, reason))


@pytest.fixture
def live_lease(monkeypatch):
    from app import web_miniapp

    store = _LeaseStore()
    socket = _Socket()
    monkeypatch.setattr("app.cache.redis_client", store)
    monkeypatch.setattr("quart.websocket", socket)
    monkeypatch.setattr(web_miniapp, "settings", SimpleNamespace(TELEGRAM_BOT_TOKEN="synthetic"))
    monkeypatch.setattr(web_miniapp, "_validate_init_data", lambda *args: {"user": {"id": 777}})
    monkeypatch.setattr(web_miniapp, "_require_authorized_websocket_user", AsyncMock(return_value=True))
    monkeypatch.setattr(web_miniapp, "_vertex_live_enabled", lambda: True)
    monkeypatch.setattr(web_miniapp, "ACTIVE_LIVE_SESSIONS", set())
    return web_miniapp, store, socket


@pytest.mark.asyncio
@pytest.mark.parametrize("first_route", ["live_audio_ws", "live_vertex_audio_ws"])
async def test_live_without_redis_excludes_both_routes_until_owner_finishes(live_lease, monkeypatch, first_route):
    module, _, socket = live_lease
    monkeypatch.setattr("app.cache.redis_client", None)
    entered = asyncio.Event()
    finished = asyncio.Event()
    session_calls = 0

    async def session(*args, **kwargs):
        nonlocal session_calls
        session_calls += 1
        if session_calls == 1:
            entered.set()
            await finished.wait()

    monkeypatch.setattr(module, "_handle_live_session", session)
    first = asyncio.create_task(getattr(module, first_route)())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await module.live_audio_ws()
        await module.live_vertex_audio_ws()
        assert session_calls == 1
        assert [code for code, _ in socket.closed] == [4009, 4009]
    finally:
        finished.set()
        await first

    await module.live_audio_ws()
    assert session_calls == 2


@pytest.mark.asyncio
async def test_live_without_redis_holds_admission_during_cancelled_handler_cleanup(live_lease, monkeypatch):
    module, _, socket = live_lease
    monkeypatch.setattr("app.cache.redis_client", None)
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_allowed = asyncio.Event()
    session_calls = 0

    async def session(*args, **kwargs):
        nonlocal session_calls
        session_calls += 1
        if session_calls == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_allowed.wait()

    monkeypatch.setattr(module, "_handle_live_session", session)
    owner = asyncio.create_task(module.live_audio_ws())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        owner.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        await module.live_vertex_audio_ws()
        assert session_calls == 1
        assert [code for code, _ in socket.closed] == [4009]
    finally:
        cleanup_allowed.set()
        await asyncio.gather(owner, return_exceptions=True)

    assert owner.cancelled()
    await module.live_vertex_audio_ws()
    assert session_calls == 2


@pytest.mark.asyncio
async def test_live_without_redis_releases_admission_after_setup_failure(live_lease, monkeypatch):
    module, _, _ = live_lease
    monkeypatch.setattr("app.cache.redis_client", None)
    session = AsyncMock(side_effect=[RuntimeError("synthetic setup failure"), None])
    monkeypatch.setattr(module, "_handle_live_session", session)

    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        await module.live_audio_ws()
    await module.live_vertex_audio_ws()
    assert session.await_count == 2


@pytest.mark.asyncio
async def test_live_redis_acquisition_failure_does_not_admit_local_fallback(live_lease, monkeypatch):
    module, store, _ = live_lease
    monkeypatch.setattr(store, "set", AsyncMock(side_effect=ConnectionError("synthetic Redis failure")))
    session = AsyncMock()
    monkeypatch.setattr(module, "_handle_live_session", session)

    with pytest.raises(ConnectionError, match="synthetic Redis failure"):
        await module.live_audio_ws()
    session.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_session_still_excludes_second_connection_after_fifteen_minutes(live_lease, monkeypatch):
    module, store, socket = live_lease
    entered = asyncio.Event()
    finished = asyncio.Event()
    active_sessions = 0

    async def session(*args, **kwargs):
        nonlocal active_sessions
        active_sessions += 1
        if active_sessions == 1:
            entered.set()
            await finished.wait()

    monkeypatch.setattr(module, "_handle_live_session", session)
    first = asyncio.create_task(module._open_authenticated_live_socket("standard"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        store.now = 901
        await module._open_authenticated_live_socket("standard")
        assert active_sessions == 1
        assert [code for code, _ in socket.closed] == [4009]
    finally:
        finished.set()
        await first


@pytest.mark.asyncio
async def test_finishing_old_live_session_preserves_replacement_owner(live_lease, monkeypatch):
    module, store, _ = live_lease

    async def session(*args, **kwargs):
        store.values["live_session:777"] = ("replacement-owner", 9999)

    monkeypatch.setattr(module, "_handle_live_session", session)
    await module._open_authenticated_live_socket("standard")
    assert store.values["live_session:777"] == ("replacement-owner", 9999)


@pytest.mark.asyncio
async def test_live_deadline_drains_handler_before_releasing_lease(live_lease, monkeypatch):
    module, store, socket = live_lease
    cleanup_owned = []

    async def session(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_owned.append("live_session:777" in store.values)

    monkeypatch.setattr(module, "_handle_live_session", session)
    monkeypatch.setattr(module, "_LIVE_SESSION_MAX_SECONDS", 0.01, raising=False)
    await asyncio.wait_for(module._open_authenticated_live_socket("standard"), 0.5)
    assert cleanup_owned == [True]
    assert store.values == {}
    assert [code for code, _ in socket.closed] == [1008]


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_owned_lease_release(live_lease, monkeypatch):
    module, store, _ = live_lease
    entered = asyncio.Event()
    handler_drained = asyncio.Event()
    store.release_allowed.clear()

    async def session(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            handler_drained.set()

    monkeypatch.setattr(module, "_handle_live_session", session)
    owner = asyncio.create_task(module._open_authenticated_live_socket("standard"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        owner.cancel()
        await asyncio.wait_for(store.release_started.wait(), 0.5)
        owner.cancel()
        await asyncio.sleep(0)
        assert handler_drained.is_set()
        assert not owner.done()
        store.release_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert store.values == {}
    finally:
        store.release_allowed.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
