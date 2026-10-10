"""Cancellation must not orphan a claimed update before the PTB queue handoff."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart, request
from telegram import Bot

from app.observability.context import request_scope
from app.observability.ingress import WebhookOriginStore
from tests.e2e.test_webhook_lifecycle import _SECRET, _TOKEN, _payload
from tests.e2e.test_webhook_lifecycle import webhook_client as _webhook_client_fixture

webhook_client = _webhook_client_fixture


async def _checkpoint():
    """Let already scheduled task wakeups run without relying on elapsed time."""
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


class _PersistedClaims:
    """Persist SET NX before holding its acknowledgement at a deterministic barrier."""

    def __init__(self, gate_at=1, failure=None):
        self.claims = set()
        self.calls = []
        self.persisted = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.gate_at = gate_at
        self.failure = failure

    async def set(self, key, value, *, ex, nx):
        assert value == "1" and ex == 86400 and nx is True
        self.calls.append(key)
        if key in self.claims:
            return None
        self.claims.add(key)
        if len(self.calls) == self.gate_at:
            self.persisted.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            if self.failure is not None:
                raise self.failure
        return True


async def _finish_requests(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


@pytest.fixture
async def webhook_lifecycle(monkeypatch, request):
    """Own a lifecycle whose registration, shutdown and PTB stop are event-gated."""
    import bot

    app = Quart(__name__)
    application = MagicMock()
    application.bot = Bot(_TOKEN, request=MagicMock(), get_updates_request=MagicMock())
    application.update_queue = asyncio.Queue(maxsize=2)
    application.job_queue = None
    application.initialize = AsyncMock()
    application._initialized = True
    application.updater = None
    builder = MagicMock()
    for method in ("token", "request", "update_queue", "concurrent_updates"):
        getattr(builder, method).return_value = builder
    builder.build.return_value = application
    control = SimpleNamespace(
        start_entered=asyncio.Event(),
        release_start=asyncio.Event(),
        registered=asyncio.Event(),
        release_registration=asyncio.Event(),
        ready=asyncio.Event(),
        shutdown=asyncio.Event(),
        stop_entered=asyncio.Event(),
        release_stop=asyncio.Event(),
        registration_failure=None,
        order=[],
    )

    async def start():
        control.start_entered.set()
        await control.release_start.wait()

    application.start = start
    blocked_start = getattr(request, "param", None) == "blocked-start"
    if not blocked_start:
        control.release_start.set()

    async def register_webhook(*args, **kwargs):
        control.registered.set()
        await control.release_registration.wait()
        if control.registration_failure is not None:
            raise control.registration_failure

    async def wait_for_shutdown():
        control.ready.set()
        await control.shutdown.wait()

    async def stop():
        control.order.append(("stop", application.update_queue.qsize()))
        control.stop_entered.set()
        await control.release_stop.wait()

    application.stop = stop
    monkeypatch.setenv("WEBHOOK_URL", "https://webhook.example.test")
    monkeypatch.setattr(bot, "quart_app", app)
    monkeypatch.setattr(bot, "shutdown_event", SimpleNamespace(wait=wait_for_shutdown))
    monkeypatch.setattr(
        bot,
        "settings",
        SimpleNamespace(
            TELEGRAM_BOT_TOKEN=_TOKEN,
            TELEGRAM_LOCAL_SERVER_URL="",
            UPDATE_QUEUE_MAXSIZE=100,
            WEBHOOK_SECRET_TOKEN=_SECRET,
            WEBHOOK_MAX_CONNECTIONS=3,
        ),
    )
    monkeypatch.setattr(bot.Application, "builder", lambda: builder)
    monkeypatch.setattr("telegram.request.HTTPXRequest", MagicMock())
    for target in (
        "bot.commands.register",
        "bot.callbacks.register",
        "bot.messages.register",
        "app.handlers.memory_commands.register",
        "app.handlers.msg_reactions.register",
        "app.bot_instance.register_bot",
    ):
        monkeypatch.setattr(target, MagicMock())
    for target in (
        "app.bot_commands.install_public_command_menu",
        "app.repos.models_repo.sync_models_from_db",
        "app.admin_alerts.alert_admin_startup",
        "app.admin_alerts.alert_admin_shutdown",
        "app.repos.memory_autosave.drain_pending_memory_writes",
        "app.repos.memory_autosave.pre_shutdown_compact",
        "bot.stop_task_queue",
        "app.providers.close_http_clients",
        "app.search_services.close_tavily_client",
        "app.intent_router.close_http_client",
        "app.cache.shutdown_redis",
        "app.memory_manager.shutdown_memory_manager",
    ):
        monkeypatch.setattr(target, AsyncMock(return_value=0))
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: None)
    monkeypatch.setattr("app.state._pending_persists", {})
    monkeypatch.setattr("app.utils.background_tasks.get_task_manager", lambda: MagicMock())
    monkeypatch.setattr("app.webhook_dedupe.redis_client", None)
    monkeypatch.setattr(Bot, "set_webhook", register_webhook)
    control.application = application
    control.client = app.test_client()
    control.path = bot._telegram_webhook_path(_TOKEN)
    control.owner = asyncio.create_task(bot.run_bot_with_retry())
    try:
        await asyncio.wait_for((control.start_entered if blocked_start else control.registered).wait(), timeout=5)
        yield control
    finally:
        control.release_registration.set()
        control.release_start.set()
        control.shutdown.set()
        control.release_stop.set()
        await _finish_requests(control.owner)


@pytest.mark.parametrize("command, gate_at", [(False, 1), (True, 1), (True, 2)])
async def test_handler_cancellation_after_persisted_claim_finishes_one_handoff(
    webhook_client, monkeypatch, command, gate_at
):
    """Removing full-admission ownership loses the queue item after persisted SET."""
    client, application, path = webhook_client
    redis = _PersistedClaims(gate_at)
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    owners = []

    @client.app.before_request
    async def capture_handler_owner():
        owners.append(asyncio.current_task())

    payload = _payload(42)
    if command:
        payload["message"].update(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    with request_scope(request_id="request-owning-42") as context:
        posting = asyncio.create_task(client.post(path, json=payload, headers=headers))
    owner = None
    try:
        await asyncio.wait_for(redis.persisted.wait(), timeout=5)
        owner = owners[0]
        owner.cancel("first disconnect")
        await _checkpoint()
        owner.cancel("second disconnect")
        await _checkpoint()
        assert not owner.done(), "The handler exited before its claimed update reached the queue"
        assert not redis.cancelled, "Request cancellation reached the Redis operation"
        assert application.update_queue.empty()
        assert origins.consume(42) is None
        redis.release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(owner, timeout=5)
        assert cancelled.value.args == ("first disconnect",)
        assert application.update_queue.get_nowait().update_id == 42
        assert application.update_queue.empty()
        assert origins.consume(42) == {
            "request_id": "request-owning-42",
            "trace_id": "request-owning-42",
            "span_id": context.span_id,
        }
        assert origins.consume(42) is None

        # A duplicate ACK is safe only after the actual queue handoff above.
        replay = dict(payload, update_id=43) if command else payload
        response = await client.post(path, json=replay, headers=headers)
        assert response.status_code == 200
        assert application.update_queue.empty()
        assert origins.consume(replay["update_id"]) is None
        assert redis.claims == (
            {
                "telegram:webhook:update:42",
                "telegram:webhook:update:43",
                "telegram:webhook:cmd:private:123456789:1111:start",
            }
            if command
            else {"telegram:webhook:update:42"}
        )
    finally:
        redis.release.set()
        await _finish_requests(posting, owner)


async def test_cancellation_during_confirmed_local_history_still_finishes_handoff(webhook_client, monkeypatch):
    """Protecting only Redis SET leaves the confirmed-history lock gap uncovered."""
    from app.webhook_dedupe import should_accept_webhook_update

    client, application, path = webhook_client
    redis = _PersistedClaims()
    redis.release.set()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    history_locks = []
    owners = []

    async def hold_history(update_id, history, lock, **kwargs):
        if not history_locks:
            await lock.acquire()
            history_locks.append(lock)
        return await should_accept_webhook_update(update_id, history, lock, **kwargs)

    monkeypatch.setattr("app.webhook_dedupe.should_accept_webhook_update", hold_history)

    @client.app.before_request
    async def capture_owner():
        owners.append(asyncio.current_task())

    posting = asyncio.create_task(
        client.post(path, json=_payload(42), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    )
    owner = None
    try:
        await redis.persisted.wait()
        owner = owners[0]
        owner.cancel("during confirmed history")
        await _checkpoint()
        assert not owner.done()
        assert application.update_queue.empty()
        assert redis.claims == {"telegram:webhook:update:42"}
        history_locks[0].release()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("during confirmed history",)
        assert application.update_queue.get_nowait().update_id == 42
        assert origins.consume(42) is not None
    finally:
        if history_locks and history_locks[0].locked():
            history_locks[0].release()
        await _finish_requests(posting, owner)


async def test_cancelled_handler_waiting_for_admission_has_no_claim_and_can_retry(webhook_client, monkeypatch):
    """A disconnected waiter must not retain a child which later claims its identity."""
    client, application, path = webhook_client
    redis = _PersistedClaims()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    second_entered = asyncio.Event()
    owners = {}

    @client.app.before_request
    async def capture_owner():
        payload = await request.get_json()
        owners[payload["update_id"]] = asyncio.current_task()
        if payload["update_id"] == 43:
            second_entered.set()

    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    first = asyncio.create_task(client.post(path, json=_payload(42), headers=headers))
    second = None
    try:
        await redis.persisted.wait()
        second = asyncio.create_task(client.post(path, json=_payload(43), headers=headers))
        await second_entered.wait()
        owners[43].cancel("unclaimed disconnect")
        with pytest.raises(asyncio.CancelledError):
            await owners[43]
        assert redis.claims == {"telegram:webhook:update:42"}
        assert origins.consume(43) is None
        redis.release.set()
        assert (await first).status_code == 200
        assert application.update_queue.get_nowait().update_id == 42
        assert origins.consume(42) is not None
        response = await client.post(path, json=_payload(43), headers=headers)
        assert response.status_code == 200
        assert application.update_queue.get_nowait().update_id == 43
        assert application.update_queue.empty()
        assert origins.consume(43) is not None
    finally:
        redis.release.set()
        await _finish_requests(first, second, *owners.values())


@pytest.mark.parametrize("failure", [TimeoutError("unknown ACK"), ConnectionError("unknown ACK")])
async def test_unknown_ack_exception_keeps_existing_local_fallback(webhook_client, monkeypatch, failure):
    client, application, path = webhook_client
    redis = _PersistedClaims(failure=failure)
    redis.release.set()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    response = await client.post(path, json=_payload(42), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    assert response.status_code == 200
    assert application.update_queue.get_nowait().update_id == 42
    assert application.update_queue.empty()
    assert origins.consume(42) is not None
    replay = await client.post(path, json=_payload(42), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    assert replay.status_code == 200
    assert application.update_queue.empty()
    assert origins.consume(42) is None


@pytest.mark.parametrize("failure", [asyncio.QueueFull(), RuntimeError("synthetic queue fault")])
async def test_queue_failure_discards_only_the_new_origin(webhook_client, monkeypatch, failure):
    client, application, path = webhook_client
    origins = WebhookOriginStore()
    origins.remember(41)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)

    class FailingQueue(asyncio.Queue):
        def put_nowait(self, item):
            raise failure

    application.update_queue = FailingQueue(maxsize=2)
    response = await client.post(path, json=_payload(42), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    assert response.status_code == (503 if isinstance(failure, asyncio.QueueFull) else 500)
    assert application.update_queue.empty()
    assert origins.consume(42) is None
    assert origins.consume(41) is not None


async def test_failure_before_origin_preserves_an_existing_origin(webhook_client, monkeypatch):
    client, application, path = webhook_client
    origins = WebhookOriginStore()
    origins.remember(42)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)

    async def fail_before_origin(*args, **kwargs):
        raise RuntimeError("synthetic claim failure")

    monkeypatch.setattr("app.webhook_dedupe.should_accept_webhook_update", fail_before_origin)
    response = await client.post(path, json=_payload(42), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    assert response.status_code == 500
    assert application.update_queue.empty()
    assert origins.consume(42) is not None


async def test_dependency_self_cancellation_does_not_enqueue_or_leave_admission_locked(webhook_client, monkeypatch):
    """A SET dependency cancelling itself is distinct from a disconnected handler."""
    client, application, path = webhook_client
    redis = _PersistedClaims(failure=asyncio.CancelledError("dependency unknown acknowledgement"))
    redis.release.set()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    owners = []

    @client.app.before_request
    async def capture_owner():
        owners.append(asyncio.current_task())

    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    posting = asyncio.create_task(client.post(path, json=_payload(42), headers=headers))
    owner = None
    try:
        await redis.persisted.wait()
        owner = owners[0]
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await asyncio.wait_for(owner, timeout=1)
        assert cancelled.value.args == ("dependency unknown acknowledgement",)
        assert owner.cancelling() == 0, "This oracle did not cancel the handler owner"
        assert application.update_queue.empty()
        assert origins.consume(42) is None
        assert redis.claims == {"telegram:webhook:update:42"}
        # Self-cancellation still has an unknown-ACK limitation; ownership does
        # not undo a remote claim or fabricate delivery. A fresh identity works.
        response = await client.post(path, json=_payload(43), headers=headers)
        assert response.status_code == 200
        assert application.update_queue.get_nowait().update_id == 43
        assert origins.consume(43) is not None
    finally:
        await _finish_requests(posting, owner)


@pytest.mark.parametrize("registration_fault", [False, True], ids=["normal-shutdown", "registration-error-cleanup"])
async def test_lifecycle_drains_active_claim_before_stop_and_rejects_unclaimed_requests(
    webhook_lifecycle, monkeypatch, registration_fault
):
    """Bypassing either lifecycle gate lets PTB stop overtake a persisted claim."""
    control = webhook_lifecycle
    redis = _PersistedClaims()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    second_entered = asyncio.Event()
    owners = {}

    @control.client.app.before_request
    async def capture_owner():
        payload = await request.get_json()
        owners[payload["update_id"]] = asyncio.current_task()
        if payload["update_id"] == 43:
            second_entered.set()

    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    first = second = None
    cleanup_entered = asyncio.Event()

    async def observe_cleanup(*args, **kwargs):
        cleanup_entered.set()

    if registration_fault:
        monkeypatch.setattr("app.admin_alerts.alert_admin_shutdown", observe_cleanup)
    try:
        if not registration_fault:
            control.release_registration.set()
            await control.ready.wait()
        first = asyncio.create_task(control.client.post(control.path, json=_payload(42), headers=headers))
        await redis.persisted.wait()
        second = asyncio.create_task(control.client.post(control.path, json=_payload(43), headers=headers))
        await second_entered.wait()
        if registration_fault:
            control.registration_failure = RuntimeError("synthetic registration failure")
            control.release_registration.set()
            await cleanup_entered.wait()
        else:
            control.shutdown.set()
            await _checkpoint()
        assert not control.stop_entered.is_set()
        rejected = await control.client.post(control.path, json=_payload(44), headers=headers)
        assert rejected.status_code == 503
        assert redis.calls == ["telegram:webhook:update:42"]
        assert origins.consume(44) is None
        if not registration_fault:
            control.owner.cancel("first shutdown interruption")
            await _checkpoint()
            control.owner.cancel("second shutdown interruption")
            await _checkpoint()
            assert not control.owner.done()
        redis.release.set()
        assert (await first).status_code == 200
        assert (await second).status_code == 503
        await control.stop_entered.wait()
        assert control.order == [("stop", 1)]
        assert control.application.update_queue.get_nowait().update_id == 42
        assert origins.consume(42) is not None
        assert origins.consume(43) is None
        if not registration_fault:
            control.owner.cancel("third shutdown interruption")
            await _checkpoint()
            assert not control.owner.done()
        control.release_stop.set()
        if registration_fault:
            with pytest.raises(RuntimeError, match="synthetic registration failure"):
                await control.owner
        else:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await control.owner
            assert cancelled.value.args == ("first shutdown interruption",)
        after_stop = await control.client.post(control.path, json=_payload(45), headers=headers)
        assert after_stop.status_code == 503
        assert redis.calls == ["telegram:webhook:update:42"]
        assert control.application.update_queue.empty()
        assert origins.consume(45) is None
        assert all(owner.done() for owner in owners.values())
    finally:
        redis.release.set()
        control.release_registration.set()
        control.release_stop.set()
        await _finish_requests(first, second, *owners.values())


async def test_error_cleanup_closes_admission_before_awaiting_other_resources(webhook_lifecycle, monkeypatch):
    """Closing only at the PTB-stop block admits new work during resource cleanup."""
    control = webhook_lifecycle
    redis = _PersistedClaims()
    redis.release.set()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    alert_entered = asyncio.Event()
    release_alert = asyncio.Event()

    async def alert(*args, **kwargs):
        alert_entered.set()
        await release_alert.wait()

    monkeypatch.setattr("app.admin_alerts.alert_admin_shutdown", alert)
    try:
        control.registration_failure = RuntimeError("synthetic registration failure")
        control.release_registration.set()
        await alert_entered.wait()
        response = await control.client.post(
            control.path, json=_payload(42), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET}
        )
        assert response.status_code == 503
        assert redis.claims == set()
        assert control.application.update_queue.empty()
        assert origins.consume(42) is None
        assert not control.stop_entered.is_set()
    finally:
        release_alert.set()
        control.release_stop.set()
        await asyncio.gather(control.owner, return_exceptions=True)


async def test_startup_cancellation_closes_and_drains_before_existing_cleanup(webhook_lifecycle, monkeypatch):
    """Cancelling set_webhook must not leave its registered route accepting forever."""
    control = webhook_lifecycle
    redis = _PersistedClaims()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    alert_entered = asyncio.Event()
    release_alert = asyncio.Event()

    async def alert(*args, **kwargs):
        alert_entered.set()
        await release_alert.wait()

    monkeypatch.setattr("app.admin_alerts.alert_admin_shutdown", alert)
    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    posting = asyncio.create_task(control.client.post(control.path, json=_payload(42), headers=headers))
    try:
        await redis.persisted.wait()
        control.owner.cancel("first startup interruption")
        await _checkpoint()
        control.owner.cancel("second startup interruption")
        await _checkpoint()
        assert not control.owner.done(), "Startup cancellation abandoned admission and cleanup"
        assert not alert_entered.is_set(), "Cleanup ran before the claimed admission drained"
        rejected = await control.client.post(control.path, json=_payload(43), headers=headers)
        assert rejected.status_code == 503
        assert redis.calls == ["telegram:webhook:update:42"]
        assert not control.stop_entered.is_set()
        redis.release.set()
        assert (await posting).status_code == 200
        await alert_entered.wait()
        control.owner.cancel("third startup interruption")
        await _checkpoint()
        assert not control.owner.done()
        release_alert.set()
        await control.stop_entered.wait()
        assert control.order == [("stop", 1)]
        assert control.application.update_queue.get_nowait().update_id == 42
        assert origins.consume(42) is not None
        assert origins.consume(43) is None
        control.release_stop.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await control.owner
        assert cancelled.value.args == ("first startup interruption",)
        after_stop = await control.client.post(control.path, json=_payload(44), headers=headers)
        assert after_stop.status_code == 503
        assert redis.calls == ["telegram:webhook:update:42"]
        assert control.application.update_queue.empty()
    finally:
        release_alert.set()
        redis.release.set()
        control.release_stop.set()
        await _finish_requests(posting)


async def test_registration_error_cleanup_finishes_resources_after_repeated_owner_cancellation(
    webhook_lifecycle, monkeypatch
):
    """Cancellation inside an ordinary-error handler cannot abandon guarded cleanup."""
    control = webhook_lifecycle
    redis = _PersistedClaims()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    alert_entered = asyncio.Event()
    release_alert = asyncio.Event()
    resources = []
    owners = []

    @control.client.app.before_request
    async def capture_request_owner():
        owners.append(asyncio.current_task())

    async def alert(*args, **kwargs):
        resources.append("alert")
        alert_entered.set()
        await release_alert.wait()

    def resource(name):
        async def cleanup(*args, **kwargs):
            resources.append(name)
            return 0

        return cleanup

    monkeypatch.setattr("app.admin_alerts.alert_admin_shutdown", alert)
    for target, name in (
        ("app.repos.memory_autosave.drain_pending_memory_writes", "memory"),
        ("app.repos.memory_autosave.pre_shutdown_compact", "compact"),
        ("bot.stop_task_queue", "queue"),
        ("app.providers.close_http_clients", "http"),
        ("app.search_services.close_tavily_client", "tavily"),
        ("app.intent_router.close_http_client", "intent"),
        ("app.cache.shutdown_redis", "redis"),
        ("app.memory_manager.shutdown_memory_manager", "memory_manager"),
    ):
        monkeypatch.setattr(target, resource(name))
    monkeypatch.setattr("app.utils.image_utils.shutdown_image_pool", lambda: resources.append("images"))
    original_stop = control.application.stop

    async def stop():
        resources.append("stop")
        await original_stop()

    control.application.stop = stop
    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    posting = asyncio.create_task(control.client.post(control.path, json=_payload(42), headers=headers))
    try:
        await redis.persisted.wait()
        control.registration_failure = RuntimeError("synthetic registration failure")
        control.release_registration.set()
        await alert_entered.wait()
        control.owner.cancel("first exceptional-cleanup interruption")
        await _checkpoint()
        control.owner.cancel("second exceptional-cleanup interruption")
        await _checkpoint()
        assert not control.owner.done(), "Error cleanup abandoned its active admission and resources"
        assert not redis.cancelled
        assert not control.stop_entered.is_set()
        assert resources == ["alert"]
        rejected = await control.client.post(control.path, json=_payload(43), headers=headers)
        assert rejected.status_code == 503
        assert redis.calls == ["telegram:webhook:update:42"]
        release_alert.set()
        await _checkpoint()
        assert not control.stop_entered.is_set()
        redis.release.set()
        assert (await posting).status_code == 200
        await control.stop_entered.wait()
        assert control.order == [("stop", 1)]
        assert control.application.update_queue.get_nowait().update_id == 42
        assert origins.consume(42) is not None
        assert origins.consume(43) is None
        control.owner.cancel("third exceptional-cleanup interruption")
        await _checkpoint()
        assert not control.owner.done()
        control.release_stop.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await control.owner
        assert cancelled.value.args == ("first exceptional-cleanup interruption",)
        assert resources == [
            "alert",
            "memory",
            "compact",
            "queue",
            "stop",
            "http",
            "tavily",
            "intent",
            "redis",
            "images",
            "memory_manager",
        ]
        after_stop = await control.client.post(control.path, json=_payload(44), headers=headers)
        assert after_stop.status_code == 503
        assert redis.calls == ["telegram:webhook:update:42"]
        assert control.application.update_queue.empty()
    finally:
        release_alert.set()
        redis.release.set()
        control.release_stop.set()
        await _finish_requests(posting, *owners)


@pytest.mark.parametrize("webhook_lifecycle", ["blocked-start"], indirect=True)
async def test_route_is_registered_only_after_ptb_start_completes(webhook_lifecycle, monkeypatch):
    """Moving route admission before PTB start can claim work without its consumer."""
    control = webhook_lifecycle
    redis = _PersistedClaims()
    redis.release.set()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    response = await control.client.post(control.path, json=_payload(42), headers=headers)
    assert response.status_code == 404
    assert redis.calls == []
    assert control.application.update_queue.empty()
    control.release_start.set()
    await control.registered.wait()
    # Registration can be pending after PTB has started; that route is safe.
    accepted = await control.client.post(control.path, json=_payload(42), headers=headers)
    assert accepted.status_code == 200
    assert control.application.update_queue.get_nowait().update_id == 42
    invalid_secret = await control.client.post(
        control.path, json=_payload(43), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"}
    )
    assert invalid_secret.status_code == 403
    assert redis.calls == ["telegram:webhook:update:42"]
