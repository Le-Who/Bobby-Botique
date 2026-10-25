"""Critical startup/worker exits must retire their lifecycle owner offline."""

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("critical", ["bot", "server"])
@pytest.mark.parametrize("exit_kind", ["failure", "return", "cancel"])
async def test_critical_child_exit_stops_and_awaits_siblings(monkeypatch, critical, exit_kind):
    import bot

    shutdown = asyncio.Event()
    release = asyncio.Event()
    sibling_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    sibling_finished = asyncio.Event()
    critical_finished = asyncio.Event()

    async def critical_worker():
        await sibling_started.wait()
        critical_finished.set()
        if exit_kind == "failure":
            raise ValueError("synthetic critical worker failure")
        if exit_kind == "cancel":
            raise asyncio.CancelledError

    async def sibling():
        sibling_started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release.wait()
            sibling_finished.set()

    async def idle(*args, **kwargs):
        await asyncio.Future()

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(bot, "settings", SimpleNamespace(PORT=19000, ENABLE_WEB_SERVER=True))
    monkeypatch.setattr(bot, "shutdown_event", shutdown)
    monkeypatch.setattr(bot, "basic_monitoring", idle)
    monkeypatch.setattr(bot, "bot_watchdog", idle)
    monkeypatch.setattr(bot, "run_bot_with_retry", critical_worker if critical == "bot" else sibling)
    monkeypatch.setattr(bot, "serve", lambda *args, **kwargs: critical_worker() if critical == "server" else sibling())
    monkeypatch.setattr("app.voice_engine.get_voice_reply_manager", lambda: SimpleNamespace(close=noop))
    monkeypatch.setattr(bot, "stop_task_queue", noop)
    monkeypatch.setattr(bot, "_drain_request_tasks", noop)
    monkeypatch.setattr(bot, "_flush_request_persistence", noop)
    monkeypatch.setattr("app.providers.close_http_clients", noop)

    owner = asyncio.create_task(bot.run_bot_and_server())
    try:
        await critical_finished.wait()
        # A critical exit must start cleanup without an external shutdown signal.
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not owner.done(), "failure escaped before sibling cancellation cleanup"
        release.set()
        expected = ValueError if exit_kind == "failure" else RuntimeError
        with pytest.raises(expected):
            await owner
        assert sibling_finished.is_set(), "failure escaped before sibling cancellation cleanup"
    finally:
        release.set()
        shutdown.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_failed_database_initialization_propagates_after_cleanup(monkeypatch):
    import bot

    failure = RuntimeError("synthetic schema validation failure")
    service_started = False
    database_closed = False

    async def fail_init():
        raise failure

    async def start_services():
        nonlocal service_started
        service_started = True

    async def close_database():
        nonlocal database_closed
        database_closed = True

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(bot.database, "init_db", fail_init)
    monkeypatch.setattr(bot.database.db_manager, "close", close_database)
    monkeypatch.setattr(bot, "install_asyncio_exception_handler", lambda: None)
    monkeypatch.setattr(bot, "run_bot_and_server", start_services)
    monkeypatch.setattr(bot, "initialize_group_chats", noop)
    monkeypatch.setattr("app.memory_manager.memory_manager.start", lambda: None)
    monkeypatch.setattr("app.memory_manager.memory_manager.stop", noop)
    monkeypatch.setattr("app.memory_manager.memory_manager.add_cleanup_callback", lambda callback: None)
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: None)

    with pytest.raises(RuntimeError) as caught:
        await bot.main()

    assert caught.value is failure
    assert not service_started, "bot/web started after schema initialization failed"
    assert database_closed, "partial database resources must close after failed startup"
