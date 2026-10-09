"""Error and cancellation cleanup orchestration, without live Telegram (CORE-G03)."""

import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("fault", ["alert", "memory", "queue", "updater", "application", "http", "redis", "images"])
async def test_application_cleanup_contains_faults_and_preserves_resource_order(monkeypatch, fault):
    import bot

    calls = []

    def action(name):
        async def run(*args, **kwargs):
            calls.append(name)
            if name == fault:
                raise RuntimeError("synthetic cleanup fault")
            return 0

        return run

    application = SimpleNamespace(
        updater=SimpleNamespace(stop=action("updater")), _initialized=True, stop=action("application")
    )
    for target, name in (
        ("app.admin_alerts.alert_admin_shutdown", "alert"),
        ("app.repos.memory_autosave.drain_pending_memory_writes", "memory"),
        ("app.repos.memory_autosave.pre_shutdown_compact", "compact"),
        ("bot.stop_task_queue", "queue"),
        ("app.providers.close_http_clients", "http"),
        ("app.search_services.close_tavily_client", "tavily"),
        ("app.intent_router.close_http_client", "intent"),
        ("app.cache.shutdown_redis", "redis"),
        ("app.memory_manager.shutdown_memory_manager", "memory_manager"),
    ):
        monkeypatch.setattr(target, action(name))
    monkeypatch.setattr("app.state._pending_persists", {})

    def images():
        calls.append("images")
        if fault == "images":
            raise RuntimeError("synthetic image shutdown fault")

    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", images)
    await bot._cleanup_application(application, reason="contract fault")
    want = [
        "alert",
        "memory",
        "compact",
        "queue",
        "updater",
        "application",
        "http",
        "tavily",
        "intent",
        "redis",
        "images",
        "memory_manager",
    ]
    if fault == "memory":
        want.remove("compact")
    assert calls == want


async def test_queue_cleanup_timeout_awaits_finally_and_continues_other_resources(monkeypatch):
    import bot

    calls = []
    cleaned = asyncio.Event()
    real_wait_for = asyncio.wait_for

    async def noop(*args, **kwargs):
        return 0

    async def queue():
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def immediate_timeout(awaitable, timeout):
        assert timeout == 10.0
        return await real_wait_for(awaitable, timeout=0.01)

    async def stopped():
        assert cleaned.is_set(), "later resource cleanup must not precede queue cancellation finalizers"
        calls.append("application")

    for target in (
        "app.admin_alerts.alert_admin_shutdown",
        "app.repos.memory_autosave.drain_pending_memory_writes",
        "app.repos.memory_autosave.pre_shutdown_compact",
        "app.providers.close_http_clients",
        "app.search_services.close_tavily_client",
        "app.intent_router.close_http_client",
        "app.cache.shutdown_redis",
        "app.memory_manager.shutdown_memory_manager",
    ):
        monkeypatch.setattr(target, noop)
    monkeypatch.setattr("app.state._pending_persists", {})
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: calls.append("images"))
    monkeypatch.setattr(bot, "stop_task_queue", queue)
    monkeypatch.setattr(bot.asyncio, "wait_for", immediate_timeout)
    await bot._cleanup_application(SimpleNamespace(updater=None, _initialized=True, stop=stopped))
    assert calls == ["application", "images"]


@pytest.mark.parametrize("failures", [0, 2, 3])
async def test_cleanup_retry_is_bounded_and_backoff_is_ordered(monkeypatch, failures):
    import bot

    actions = []
    attempts = 0

    async def cleanup():
        nonlocal attempts
        attempts += 1
        actions.append("attempt")
        if attempts <= failures:
            raise RuntimeError("synthetic resource fault")

    async def delay(seconds):
        actions.append(seconds)

    monkeypatch.setattr(bot.asyncio, "sleep", delay)
    await bot._cleanup_with_retry("contract", cleanup, retries=3, base_delay=0.25)
    assert attempts == (1 if failures == 0 else 3)
    assert actions == (["attempt"] if failures == 0 else ["attempt", 0.25, "attempt", 0.5, "attempt"])


@pytest.mark.parametrize("save_timeout", [False, True], ids=["flush", "timeout"])
async def test_cleanup_flushes_evicted_pending_state_before_stopping_dependents(monkeypatch, save_timeout):
    import bot
    from app import state

    calls = []
    store = state._UserStateStore(maxsize=1)
    monkeypatch.setattr(state, "USER_STATES", store)
    monkeypatch.setattr(state, "_pending_persists", {})
    monkeypatch.setattr(state, "_PERSIST_DEBOUNCE_SEC", 60)
    real_wait_for = asyncio.wait_for

    async def wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout=0.01 if timeout == 5.0 else 1)

    async def save(**data):
        assert data["user_id"] == 11 and data["selected_document_id"] == 42
        calls.append("save_started")
        try:
            if save_timeout:
                await asyncio.Future()
            calls.append("save_finished")
        finally:
            await asyncio.sleep(0)
            calls.append("save_cleanup")

    async def queue():
        assert calls[-1] == "save_cleanup"
        calls.append("queue")

    async def redis():
        calls.append("redis")

    async def noop(*args, **kwargs):
        return 0

    for target in (
        "app.admin_alerts.alert_admin_shutdown",
        "app.repos.memory_autosave.drain_pending_memory_writes",
        "app.repos.memory_autosave.pre_shutdown_compact",
        "app.providers.close_http_clients",
        "app.search_services.close_tavily_client",
        "app.intent_router.close_http_client",
        "app.memory_manager.shutdown_memory_manager",
    ):
        monkeypatch.setattr(target, noop)
    monkeypatch.setattr("app.repos.users.save_user_state", save)
    monkeypatch.setattr("app.cache.shutdown_redis", redis)
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: None)
    monkeypatch.setattr(bot, "stop_task_queue", queue)
    monkeypatch.setattr(bot.asyncio, "wait_for", wait_for)
    state.set_document_mode(11, True, document_id=42)
    handle = state._pending_persists[11]
    store[12]
    await bot._cleanup_application(SimpleNamespace(updater=None, _initialized=False))
    assert calls == (
        ["save_started", "save_cleanup", "queue", "redis"]
        if save_timeout
        else ["save_started", "save_finished", "save_cleanup", "queue", "redis"]
    )
    assert handle.cancelled()
    assert not state._pending_persists


@pytest.mark.parametrize("web, server_fault", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("drain_outcome", ["success", "incomplete", "fault"])
@pytest.mark.parametrize("trigger", ["signal", "cancel"])
async def test_orchestration_collects_failed_children_and_awaits_sibling_cleanup(
    monkeypatch, web, server_fault, drain_outcome, trigger
):
    import bot

    shutdown = asyncio.Event()
    bot_failed = asyncio.Event()
    started = {name: asyncio.Event() for name in ("monitor", "watchdog", "server")}
    finished = set()
    tasks = []
    release = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def child(name):
        tasks.append(asyncio.current_task())
        started[name].set()
        if name == "server" and server_fault:
            raise RuntimeError("synthetic server failure")
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release.wait()
            finished.add(name)

    async def failed_bot():
        tasks.append(asyncio.current_task())
        bot_failed.set()
        raise RuntimeError("synthetic bot failure")

    async def drain(**kwargs):
        assert kwargs == {"timeout": 10.0}
        if drain_outcome == "fault":
            raise RuntimeError("synthetic background drain fault")
        return drain_outcome == "success"

    monkeypatch.setattr(bot, "settings", SimpleNamespace(PORT=19000, ENABLE_WEB_SERVER=web))
    monkeypatch.setattr(bot, "shutdown_event", shutdown)
    monkeypatch.setattr(bot, "basic_monitoring", lambda: child("monitor"))
    monkeypatch.setattr(bot, "run_bot_with_retry", failed_bot)
    monkeypatch.setattr(bot, "bot_watchdog", lambda task: child("watchdog"))
    monkeypatch.setattr(bot, "serve", lambda *args, **kwargs: child("server"))
    monkeypatch.setattr("app.utils.background_tasks.get_task_manager", lambda: SimpleNamespace(drain=drain))
    owner = asyncio.create_task(bot.run_bot_and_server())
    try:
        await bot_failed.wait()
        await started["monitor"].wait()
        await started["watchdog"].wait()
        if web:
            await started["server"].wait()
        assert not owner.done(), "watchdog logs failures; shutdown remains explicitly signalled"
        if trigger == "signal":
            shutdown.set()
        else:
            owner.cancel()
        await cleanup_started.wait()
        assert not owner.done()
        release.set()
        if trigger == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 1)
        else:
            await asyncio.wait_for(owner, 1)
        assert finished == ({"monitor", "watchdog", "server"} if web and not server_fault else {"monitor", "watchdog"})
        assert all(task.done() for task in tasks)
        assert sum(isinstance(task.exception(), RuntimeError) for task in tasks if not task.cancelled()) == (
            2 if server_fault else 1
        )
    finally:
        release.set()
        shutdown.set()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
