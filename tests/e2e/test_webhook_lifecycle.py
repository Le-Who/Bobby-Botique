"""Exercise the webhook registered by the production bot lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart, request
from telegram import Bot, Update

from app.web import quart_app

_TOKEN = "123456789:ABCDefghIJKlmnOPQRstuVWXyz"
_SECRET = "synthetic-webhook-secret"


@pytest.fixture
async def webhook_client(monkeypatch):
    import bot

    test_app = Quart(__name__)
    application = MagicMock()
    application.bot = Bot(_TOKEN, request=MagicMock(), get_updates_request=MagicMock())
    application.update_queue = asyncio.Queue(maxsize=2)
    application.job_queue = None
    application.initialize = AsyncMock()
    application.start = AsyncMock()
    application.stop = AsyncMock()
    application.process_update = AsyncMock()
    builder = MagicMock()
    for method in ("token", "request", "update_queue", "concurrent_updates"):
        getattr(builder, method).return_value = builder
    builder.build.return_value = application

    ready = asyncio.Event()
    shutdown = asyncio.Event()

    async def wait_for_shutdown():
        ready.set()
        await shutdown.wait()

    monkeypatch.setenv("WEBHOOK_URL", "https://webhook.example.test")
    monkeypatch.setattr(bot, "quart_app", test_app)
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
    monkeypatch.setattr("app.bot_commands.install_public_command_menu", AsyncMock())
    monkeypatch.setattr("app.repos.models_repo.sync_models_from_db", AsyncMock())
    monkeypatch.setattr("app.admin_alerts.alert_admin_startup", AsyncMock())
    monkeypatch.setattr("app.utils.background_tasks.get_task_manager", lambda: MagicMock())
    monkeypatch.setattr("app.webhook_dedupe.redis_client", None)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", MagicMock())
    monkeypatch.setattr(Bot, "set_webhook", AsyncMock())

    task = asyncio.create_task(bot.run_bot_with_retry())
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        yield test_app.test_client(), application, bot._telegram_webhook_path(_TOKEN)
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=5)


def _payload(update_id=987654321):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1111,
            "date": 1690000000,
            "chat": {"id": 123456789, "type": "private"},
            "text": "hello",
        },
    }


class _RedisClaims:
    """Stateful SET NX transport double; outage occurs before any write."""

    def __init__(self):
        self.claims = set()
        self.available = True

    async def set(self, key, value, *, ex, nx):
        assert value == "1" and ex > 0 and nx is True
        if not self.available:
            raise ConnectionError("synthetic Redis outage")
        if key in self.claims:
            return None
        self.claims.add(key)
        return True


@pytest.mark.parametrize("command", [False, True])
async def test_confirmed_redis_update_is_not_requeued_during_outage(webhook_client, monkeypatch, command):
    from app.observability.ingress import WebhookOriginStore

    client, application, path = webhook_client
    redis = _RedisClaims()
    origins = WebhookOriginStore()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.observability.ingress.webhook_origins", origins)
    payload = _payload(42)
    if command:
        payload["message"].update(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}

    accepted = await client.post(path, json=payload, headers=headers)
    assert accepted.status_code == 200
    assert application.update_queue.get_nowait().update_id == 42
    assert application.update_queue.empty()
    assert origins.consume(42) is not None

    redis.available = False
    replay = dict(payload, update_id=43) if command else payload
    duplicate = await client.post(path, json=replay, headers=headers)
    assert duplicate.status_code == 200
    assert application.update_queue.empty(), "Confirmed Redis admission was queued again in local fallback"
    assert origins.consume(replay["update_id"]) is None
    application.process_update.assert_not_awaited()

    # A distinct message remains deliverable while Redis is down.
    fresh = _payload(44)
    fresh["message"]["message_id"] = 1112
    if command:
        fresh["message"].update(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    response = await client.post(path, json=fresh, headers=headers)
    assert response.status_code == 200
    assert application.update_queue.get_nowait().update_id == 44
    assert application.update_queue.empty()
    assert origins.consume(44) is not None


async def test_webhook_valid_payload_is_queued(webhook_client):
    client, application, path = webhook_client
    response = await client.post(path, json=_payload(), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    assert response.status_code == 200
    queued = application.update_queue.get_nowait()
    assert isinstance(queued, Update)
    assert queued.update_id == 987654321
    assert queued.message.text == "hello"
    application.process_update.assert_not_awaited()
    application.bot.set_webhook.assert_awaited_once_with(
        url=f"https://webhook.example.test{path}",
        allowed_updates=[
            "message",
            "edited_message",
            "callback_query",
            "inline_query",
            "chosen_inline_result",
            "message_reaction",
        ],
        drop_pending_updates=True,
        max_connections=3,
        secret_token=_SECRET,
    )


@pytest.mark.parametrize("secret", ["", "wrong-secret"])
async def test_webhook_rejects_missing_or_wrong_secret(webhook_client, secret):
    client, application, path = webhook_client
    response = await client.post(path, json=_payload(), headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert response.status_code == 403
    assert application.update_queue.empty()


async def test_webhook_unregistered_path_returns_404(webhook_client):
    client, application, _ = webhook_client
    response = await client.post("/webhook/unregistered", json=_payload())
    assert response.status_code == 404
    assert application.update_queue.empty()


@pytest.mark.parametrize("body", ["NOT VALID JSON {", "[]", "null"])
async def test_webhook_malformed_json_returns_400(webhook_client, body):
    client, application, path = webhook_client
    response = await client.post(
        path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": _SECRET,
        },
    )
    assert response.status_code == 400
    assert application.update_queue.empty()


async def test_webhook_invalid_update_returns_400(webhook_client):
    client, application, path = webhook_client
    response = await client.post(
        path,
        json={"update_id": 12, "message": {"chat": {"id": "invalid", "type": "private"}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET},
    )
    assert response.status_code == 400
    assert application.update_queue.empty()


async def test_webhook_duplicate_is_acknowledged_without_second_queue_item(webhook_client):
    client, application, path = webhook_client
    for _ in range(2):
        response = await client.post(path, json=_payload(), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
        assert response.status_code == 200
    assert application.update_queue.qsize() == 1


async def test_webhook_full_queue_returns_retryable_503(webhook_client):
    from app.observability.ingress import webhook_origins

    client, application, path = webhook_client
    for _ in range(application.update_queue.maxsize):
        application.update_queue.put_nowait(object())
    response = await client.post(path, json=_payload(), headers={"X-Telegram-Bot-Api-Secret-Token": _SECRET})
    assert response.status_code == 503
    assert application.update_queue.full()
    webhook_origins.remember.assert_not_called()
    webhook_origins.discard.assert_not_called()


@pytest.mark.parametrize("command", [False, True])
async def test_webhook_retry_after_overload_is_not_lost_to_deduplication(webhook_client, command):
    client, application, path = webhook_client
    payload = _payload()
    if command:
        payload["message"].update(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    for _ in range(application.update_queue.maxsize):
        application.update_queue.put_nowait(object())
    overloaded = await client.post(path, json=payload, headers=headers)
    assert overloaded.status_code == 503
    while not application.update_queue.empty():
        application.update_queue.get_nowait()

    retried = await client.post(path, json=payload, headers=headers)
    assert retried.status_code == 200
    assert application.update_queue.qsize() == 1
    assert application.update_queue.get_nowait().update_id == payload["update_id"]


async def test_concurrent_webhooks_claim_only_an_available_queue_slot(webhook_client, monkeypatch):
    client, application, path = webhook_client
    application.update_queue.put_nowait(object())
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    calls = []
    claimed = set()

    async def redis_set(key, value, **kwargs):
        calls.append(key)
        if len(calls) == 1:
            first_entered.set()
            await release_first.wait()
        if key in claimed:
            return False
        claimed.add(key)
        return True

    monkeypatch.setattr("app.webhook_dedupe.redis_client", SimpleNamespace(set=redis_set))

    @client.app.before_request
    async def observe_second():
        payload = await request.get_json()
        if payload["update_id"] == 2:
            second_started.set()

    headers = {"X-Telegram-Bot-Api-Secret-Token": _SECRET}
    first = asyncio.create_task(client.post(path, json=_payload(1), headers=headers))
    second = None
    try:
        await asyncio.wait_for(first_entered.wait(), timeout=5)
        second = asyncio.create_task(client.post(path, json=_payload(2), headers=headers))
        await asyncio.wait_for(second_started.wait(), timeout=5)
        assert len(calls) == 1, "Second update claimed before the first admission completed"
        release_first.set()
        responses = await asyncio.gather(first, second)
        assert [response.status_code for response in responses] == [200, 503]
        assert claimed == {"telegram:webhook:update:1"}
        while not application.update_queue.empty():
            application.update_queue.get_nowait()
        retry = await client.post(path, json=_payload(2), headers=headers)
        assert retry.status_code == 200
        assert application.update_queue.qsize() == 1
        assert application.update_queue.get_nowait().update_id == 2
    finally:
        release_first.set()
        tasks = [first] + ([second] if second is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_health_endpoint_success():
    client = quart_app.test_client()
    with (
        patch("app.database.is_database_connected", return_value=True),
        patch("app.cache.ping_safe", new_callable=AsyncMock, return_value=True),
    ):
        response = await client.get("/health")
    assert response.status_code == 200
    data = await response.get_json()
    assert data["status"] == "healthy"
    assert data["services"]["database"] == "connected"
    assert data["services"]["bot"] == "running"
