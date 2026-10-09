"""Config reload ownership with overdue synchronous readers and async watchers."""

import asyncio
import gc

import pytest

from app import config


def isolated_manager(monkeypatch):
    cached = config.Settings.model_construct(
        TELEGRAM_BOT_TOKEN="123:synthetic",
        DATABASE_URL="postgresql://synthetic.invalid/test",
        ADMIN_ID=71,
        DEFAULT_MODEL="synthetic-old-model",
    )
    monkeypatch.setattr(config, "get_settings_safe", lambda: cached)
    monkeypatch.setattr(config.time, "time", lambda: 600.0)
    # The real reload changes these globals; retain pytest ownership for restore.
    monkeypatch.setattr(config, "_settings_instance", cached)
    monkeypatch.setattr(config, "settings", cached)
    manager = config.ConfigManager()
    manager._last_reload = 0.0
    return manager, cached


def test_overdue_sync_setting_read_does_not_abandon_reload_coroutine(monkeypatch, recwarn):
    # Constructing the reload coroutine before checking for a running loop leaks
    # it when an ordinary synchronous reader first crosses the reload deadline.
    manager, cached = isolated_manager(monkeypatch)
    reads = []

    def load_settings():
        reads.append("external read")
        raise AssertionError("Synchronous setting reads must not reload external config")

    monkeypatch.setattr(config, "load_settings", load_settings)
    assert manager.get_setting("DEFAULT_MODEL") == "synthetic-old-model"
    assert manager.settings is cached
    assert manager._reload_task is None
    assert reads == []
    gc.collect()
    assert [(item.category, str(item.message)) for item in recwarn] == []


def test_overdue_sync_reader_keeps_next_async_reload_opportunity(monkeypatch, recwarn):
    # A synchronous read cannot mark a reload as scheduled when no loop exists:
    # the next async read at the same time still has to refresh the cached value.
    manager, cached = isolated_manager(monkeypatch)
    refreshed = cached.model_copy(deep=True)
    refreshed.DEFAULT_MODEL = "synthetic-new-model"
    reads = []

    def load_settings():
        reads.append("external read")
        return refreshed

    monkeypatch.setattr(config, "load_settings", load_settings)
    assert manager.settings is cached
    assert reads == []

    async def next_reader():
        assert manager.settings is cached
        reload_task = manager._reload_task
        assert reload_task is not None
        await asyncio.wait_for(reload_task, 2)

    asyncio.run(next_reader())
    assert reads == ["external read"]
    assert cached.DEFAULT_MODEL == "synthetic-new-model"
    gc.collect()
    assert [(item.category, str(item.message)) for item in recwarn] == []


@pytest.mark.asyncio
async def test_overdue_async_reader_owns_one_reload_until_watcher_finishes(monkeypatch):
    # A valid async loop must still refresh once, preserve shared identity and
    # retain the task until a blocked watcher completes; readers cannot overlap it.
    manager, cached = isolated_manager(monkeypatch)
    refreshed = cached.model_copy(deep=True)
    refreshed.DEFAULT_MODEL = "synthetic-new-model"
    reads = []
    entered = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()
    notifications = []

    def load_settings():
        reads.append("external read")
        return refreshed

    async def watcher(old, new):
        notifications.append((old.DEFAULT_MODEL, new.DEFAULT_MODEL))
        entered.set()
        try:
            await release.wait()
        finally:
            exited.set()

    monkeypatch.setattr(config, "load_settings", load_settings)
    manager.add_watcher(watcher)
    assert manager.settings is cached
    reload_task = manager._reload_task
    assert reload_task is not None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert cached.DEFAULT_MODEL == "synthetic-new-model"
        assert not reload_task.done()
        assert not exited.is_set()
        # Cross another interval while the first task still owns the watcher.
        monkeypatch.setattr(config.time, "time", lambda: 1200.0)
        assert manager.get_setting("DEFAULT_MODEL") == "synthetic-new-model"
        assert manager._reload_task is reload_task
        assert reads == ["external read"]
        assert notifications == [("synthetic-old-model", "synthetic-new-model")]
        release.set()
        await asyncio.wait_for(reload_task, 2)
    finally:
        if not reload_task.done():
            reload_task.cancel()
        await asyncio.gather(reload_task, return_exceptions=True)
    assert reload_task.done()
    assert exited.is_set()
    assert config.settings is cached
    assert config._settings_instance is cached


@pytest.mark.asyncio
async def test_cancelled_reload_awaits_watcher_cleanup_before_task_returns(monkeypatch):
    # Runtime cancellation must propagate through the watcher and finish its async
    # finally before the manager's tracked task reports completion.
    manager, cached = isolated_manager(monkeypatch)
    monkeypatch.setattr(config, "load_settings", lambda: cached.model_copy(deep=True))
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def watcher(_old, _new):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            exited.set()

    manager.add_watcher(watcher)
    assert manager.settings is cached
    reload_task = manager._reload_task
    assert reload_task is not None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task
        assert reload_task.done()
        assert exited.is_set()
    finally:
        if not reload_task.done():
            reload_task.cancel()
        await asyncio.gather(reload_task, return_exceptions=True)
