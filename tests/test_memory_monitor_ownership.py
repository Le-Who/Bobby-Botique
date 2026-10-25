"""The process monitor owns one lifecycle and samples outside the event loop."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


async def test_periodic_memory_sampling_runs_outside_event_loop(monkeypatch):
    from app.memory_manager import MemoryManager

    manager = MemoryManager()
    sampled_threads = []
    owner_thread = threading.get_ident()

    def sample():
        sampled_threads.append(threading.get_ident())
        return {"percent": 50}

    monkeypatch.setattr(manager, "_get_memory_info", sample)
    try:
        await manager._check_memory_usage()
        assert len(sampled_threads) == 1
        assert sampled_threads[0] != owner_thread
        assert manager._memory_history == [{"percent": 50}]
    finally:
        await manager.stop()


async def test_main_starts_and_stops_the_shared_monitor(monkeypatch):
    import bot
    from app import memory_manager as module

    shared = SimpleNamespace(start=Mock(), add_cleanup_callback=Mock(), stop=AsyncMock())
    constructor = Mock(return_value=SimpleNamespace(add_cleanup_callback=Mock(), stop=AsyncMock()))
    monkeypatch.setattr(module, "memory_manager", shared)
    monkeypatch.setattr(module, "MemoryManager", constructor)
    monkeypatch.setattr(bot, "install_asyncio_exception_handler", lambda: None)
    # Exercise the monitor's successful startup/shutdown path with synthetic
    # infrastructure; failed database initialization now correctly stops main.
    monkeypatch.setattr(bot.database, "init_db", AsyncMock())
    monkeypatch.setattr("app.runtime_settings.lifecycle.refresh_runtime_settings", AsyncMock())
    monkeypatch.setattr("app.config.config_manager.add_watcher", Mock())
    monkeypatch.setattr(bot, "start_task_queue", AsyncMock())
    monkeypatch.setattr(bot, "stop_task_queue", AsyncMock())
    monkeypatch.setattr(bot, "startup_health_check", AsyncMock())
    monkeypatch.setattr(bot.metrics_collector, "cleanup", AsyncMock())
    monkeypatch.setattr(bot.database.db_manager, "close", AsyncMock())
    monkeypatch.setattr(bot, "initialize_group_chats", AsyncMock())
    monkeypatch.setattr(bot, "run_bot_and_server", AsyncMock())
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: None)
    # Keep this lifecycle test independent of host signal registration.
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)

    await bot.main()

    constructor.assert_not_called()
    shared.start.assert_called_once_with()
    shared.add_cleanup_callback.assert_called_once()
    shared.stop.assert_awaited_once_with()
