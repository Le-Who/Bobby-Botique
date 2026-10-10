"""Real Redis claims survive caller cancellation until queue handoff and stop."""

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from app.observability.ingress import WebhookOriginStore
from app.webhook_admission import AdmissionClosed, WebhookAdmissionGate
from app.webhook_dedupe import should_accept_webhook_update

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _checkpoint():
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await reached.wait()


@pytest.fixture
async def redis_claims():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("Explicit TEST_REDIS_URL must identify a disposable Redis")
    writer = Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    observer = Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    own_keys = set()
    try:
        assert await writer.ping()
        assert await observer.ping()
        yield writer, observer, own_keys
    finally:
        try:
            if own_keys:
                await observer.delete(*own_keys)
        finally:
            await writer.aclose()
            await observer.aclose()


class _AfterRealSet:
    """Hold only the acknowledgement after the real Redis SET has returned."""

    def __init__(self, client, gate_at):
        self.client = client
        self.gate_at = gate_at
        self.persisted = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.cancelled = False

    async def set(self, key, value, *, ex, nx):
        self.calls.append(key)
        result = await self.client.set(key, value, ex=ex, nx=nx)
        if result and len(self.calls) == self.gate_at:
            self.persisted.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return result


@pytest.mark.parametrize("command, gate_at", [(False, 1), (True, 1), (True, 2)])
@pytest.mark.parametrize("shutdown_during_claim", [False, True], ids=["request-cancel", "request-and-shutdown-cancel"])
async def test_persisted_redis_claim_finishes_one_handoff_before_cancellation_and_stop(
    redis_claims, monkeypatch, command, gate_at, shutdown_during_claim
):
    writer, observer, own_keys = redis_claims
    unique = int(uuid.uuid4().hex[:15], 16)
    update_id, chat_id, message_id = unique, unique + 2, unique + 3
    update_key = f"telegram:webhook:update:{update_id}"
    replay_key = f"telegram:webhook:update:{update_id + 1}"
    rejected_key = f"telegram:webhook:update:{update_id + 4}"
    command_key = f"telegram:webhook:cmd:private:{chat_id}:{message_id}:start"
    own_keys.update((update_key, replay_key, rejected_key, command_key))
    payload = {"update_id": update_id}
    if command:
        payload["message"] = {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "private"},
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }
    transport = _AfterRealSet(writer, gate_at)
    monkeypatch.setattr("app.webhook_dedupe.redis_client", transport)
    gate = WebhookAdmissionGate()
    history = {}
    history_lock = asyncio.Lock()
    queue = asyncio.Queue(maxsize=2)
    origins = WebhookOriginStore()
    order = []
    stop_entered = asyncio.Event()

    async def operation(data):
        if queue.full():
            return 503
        identity = data["update_id"]
        if not await should_accept_webhook_update(
            identity, history, history_lock, payload=data, ttl_seconds=86400, capacity=10000
        ):
            return 200
        origins.remember(identity)
        queue.put_nowait(identity)
        order.append("handoff")
        return 200

    async def stop():
        assert queue.qsize() == 1
        order.append("stop")
        stop_entered.set()

    owner = asyncio.create_task(gate.admit(lambda: operation(payload)))
    shutdown = None
    try:
        await asyncio.wait_for(transport.persisted.wait(), timeout=5)
        assert await observer.get(update_key) == "1"
        assert 0 < await observer.ttl(update_key) <= 86400
        if gate_at == 2:
            assert await observer.get(command_key) == "1"
        owner.cancel("first real Redis disconnect")
        await _checkpoint()
        owner.cancel("second real Redis disconnect")
        await _checkpoint()
        assert not owner.done()
        assert not transport.cancelled
        assert queue.empty()
        assert origins.consume(update_id) is None
        if shutdown_during_claim:
            shutdown = asyncio.create_task(gate.shutdown(stop))
            await _checkpoint()
            shutdown.cancel("first real Redis shutdown interruption")
            await _checkpoint()
            shutdown.cancel("second real Redis shutdown interruption")
            await _checkpoint()
            assert not shutdown.done()
            assert not stop_entered.is_set()
            with pytest.raises(AdmissionClosed):
                await gate.admit(lambda: operation({"update_id": update_id + 4}))
            assert await observer.get(rejected_key) is None
        transport.release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await owner
        assert cancelled.value.args == ("first real Redis disconnect",)
        if shutdown is not None:
            with pytest.raises(asyncio.CancelledError) as cancelled_shutdown:
                await shutdown
            assert cancelled_shutdown.value.args == ("first real Redis shutdown interruption",)
            assert order == ["handoff", "stop"]
        else:
            assert order == ["handoff"]
        assert queue.get_nowait() == update_id
        assert queue.empty()
        assert origins.consume(update_id) is not None
        assert origins.consume(update_id) is None
        assert await observer.get(update_key) == "1"
        if command:
            assert await observer.get(command_key) == "1"

        # An open owner (including a replacement lifecycle) sees the actual NX
        # decision. A duplicate ACK cannot conceal a missing first queue handoff.
        replay = dict(payload, update_id=update_id + 1) if command else payload
        assert await WebhookAdmissionGate().admit(lambda: operation(replay)) == 200
        assert queue.empty()
        assert origins.consume(replay["update_id"]) is None
    finally:
        transport.release.set()
        tasks = [owner] + ([shutdown] if shutdown is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
