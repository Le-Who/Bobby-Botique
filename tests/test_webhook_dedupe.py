import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.webhook_dedupe import WebhookDedupeUnavailable, claim_webhook_update


async def should_accept_webhook_update(*args, **kwargs):
    claim = await claim_webhook_update(*args, **kwargs)
    if claim is None:
        return False
    await claim.confirm_enqueued()
    return True


class _RedisClaims:
    def __init__(self):
        self.claims = set()
        self.available = True
        self.cancel_at = None
        self.values = {}

    async def set(self, key, value, *, ex, nx):
        assert value.startswith("pending:") and ex > 0 and nx is True
        if not self.available:
            raise ConnectionError("synthetic Redis outage")
        if key in self.claims:
            return None
        self.claims.add(key)
        self.values[key] = value
        if len(self.claims) == self.cancel_at:
            raise asyncio.CancelledError
        return True

    async def get(self, key):
        return self.values.get(key, "accepted:fixture" if key in self.claims else None)

    async def eval(self, script, count, *args):
        keys, token = args[:count], args[count]
        if "ARGV[2]" in script:
            if any(self.values.get(key) != token for key in keys):
                return 0
            self.values.update(dict.fromkeys(keys, args[count + 1]))
        else:
            for key in keys:
                if self.values.get(key) == token:
                    self.values.pop(key, None)
                    self.claims.discard(key)
        return 1


def _command_payload(message_id=55):
    return {
        "message": {
            "message_id": message_id,
            "chat": {"id": 123, "type": "private"},
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }
    }


@pytest.mark.parametrize("command", [False, True])
async def test_confirmed_claim_survives_outage_until_local_ttl_expires(monkeypatch, command):
    redis = _RedisClaims()
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.webhook_dedupe.time", SimpleNamespace(monotonic=lambda: clock.now))
    seen = {}
    lock = asyncio.Lock()
    payload = _command_payload() if command else None
    assert await should_accept_webhook_update(1, seen, lock, payload=payload, ttl_seconds=4)
    redis.available = False
    clock.now = 13.9
    replay_id = 2 if command else 1
    assert not await should_accept_webhook_update(replay_id, seen, lock, payload=payload, ttl_seconds=4)
    clock.now = 14.1
    with pytest.raises(WebhookDedupeUnavailable):
        await should_accept_webhook_update(replay_id, seen, lock, payload=payload, ttl_seconds=4)
    assert seen == {}


@pytest.mark.parametrize("command", [False, True])
async def test_confirmed_history_is_bounded_and_evicts_oldest_admission(monkeypatch, command):
    redis = _RedisClaims()
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    monkeypatch.setattr("app.webhook_dedupe.time", SimpleNamespace(monotonic=lambda: clock.now))
    seen = {}
    lock = asyncio.Lock()
    for update_id in range(1, 6):
        clock.now += 1
        payload = _command_payload(update_id) if command else None
        assert await should_accept_webhook_update(update_id, seen, lock, payload=payload, capacity=4)
        assert len(seen) <= 4
    redis.available = False
    retained = _command_payload(5) if command else None
    assert not await should_accept_webhook_update(50 if command else 5, seen, lock, payload=retained, capacity=4)
    evicted = _command_payload(1) if command else None
    with pytest.raises(WebhookDedupeUnavailable):
        await should_accept_webhook_update(10 if command else 1, seen, lock, payload=evicted, capacity=4)
    assert len(seen) <= 4


async def test_command_admission_cannot_overflow_remaining_local_capacity(monkeypatch):
    redis = _RedisClaims()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    seen = {}
    lock = asyncio.Lock()
    for update_id in (1, 2, 3):
        assert await should_accept_webhook_update(update_id, seen, lock, capacity=4)
    assert await should_accept_webhook_update(4, seen, lock, payload=_command_payload(), capacity=4)
    assert len(seen) <= 4
    redis.available = False
    assert not await should_accept_webhook_update(40, seen, lock, payload=_command_payload(), capacity=4)


async def test_recovered_redis_remains_primary_for_other_processes(monkeypatch):
    redis = _RedisClaims()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    seen = {}
    lock = asyncio.Lock()
    assert await should_accept_webhook_update(1, seen, lock)
    redis.available = False
    with pytest.raises(WebhookDedupeUnavailable):
        await should_accept_webhook_update(2, seen, lock)
    assert 2 not in seen
    redis.available = True
    assert not await should_accept_webhook_update(1, seen, lock)
    assert not await should_accept_webhook_update(1, {}, asyncio.Lock())
    assert await should_accept_webhook_update(3, seen, lock)
    assert not await should_accept_webhook_update(3, {}, asyncio.Lock())


async def test_redis_reclaim_of_cached_update_keeps_command_history_bounded(monkeypatch):
    redis = _RedisClaims()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    seen = {}
    lock = asyncio.Lock()
    for update_id in (1, 2, 3, 4):
        assert await should_accept_webhook_update(update_id, seen, lock, capacity=4)
    # Redis may expire/reset independently; its confirmed claim remains primary.
    redis.claims.clear()
    assert await should_accept_webhook_update(1, seen, lock, payload=_command_payload(), capacity=4)
    assert len(seen) <= 4
    redis.available = False
    assert not await should_accept_webhook_update(10, seen, lock, payload=_command_payload(), capacity=4)


async def test_rejected_command_claim_does_not_mirror_partial_redis_admission(monkeypatch):
    redis = _RedisClaims()
    redis.claims.add("telegram:webhook:cmd:private:123:55:start")
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    seen = {}
    assert not await should_accept_webhook_update(1, seen, asyncio.Lock(), payload=_command_payload())
    assert seen == {}
    assert "telegram:webhook:update:1" not in redis.claims


@pytest.mark.parametrize("cancel_at", [1, 2])
async def test_unacknowledged_redis_write_is_not_mirrored_as_confirmed(monkeypatch, cancel_at):
    redis = _RedisClaims()
    redis.cancel_at = cancel_at
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    seen = {}
    with pytest.raises(asyncio.CancelledError):
        await should_accept_webhook_update(1, seen, asyncio.Lock(), payload=_command_payload())
    assert seen == {}
    assert len(redis.claims) == cancel_at


@pytest.mark.asyncio
async def test_webhook_update_dedupe_uses_redis_claim() -> None:
    redis = _RedisClaims()
    redis.set = AsyncMock(side_effect=redis.set)

    with patch("app.webhook_dedupe.redis_client", redis):
        assert await should_accept_webhook_update(42, {}, asyncio.Lock())
        assert not await should_accept_webhook_update(42, {}, asyncio.Lock())

    assert redis.set.await_args_list[0].args[0] == "telegram:webhook:update:42"
    assert redis.set.await_args_list[0].args[1].startswith("pending:")


@pytest.mark.asyncio
async def test_webhook_update_dedupe_falls_back_to_local_ttl_cache() -> None:
    seen: dict[int, float] = {}
    lock = asyncio.Lock()

    with patch("app.webhook_dedupe.redis_client", None):
        assert await should_accept_webhook_update(77, seen, lock)
        assert not await should_accept_webhook_update(77, seen, lock)


@pytest.mark.asyncio
async def test_webhook_command_dedupe_rejects_same_message_with_new_update_id() -> None:
    redis = _RedisClaims()
    redis.claims.add("telegram:webhook:cmd:private:123:55:start")
    payload = {
        "message": {
            "message_id": 55,
            "chat": {"id": 123, "type": "private"},
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }
    }

    with patch("app.webhook_dedupe.redis_client", redis):
        assert not await should_accept_webhook_update(1001, {}, asyncio.Lock(), payload=payload)


@pytest.mark.parametrize("stored", ["1", "pending:other-process"])
async def test_pending_or_legacy_claim_never_acknowledges_an_unqueued_update(monkeypatch, stored):
    redis = _RedisClaims()
    key = "telegram:webhook:update:42"
    redis.claims.add(key)
    redis.values[key] = stored
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    for _ in range(2):
        with pytest.raises(WebhookDedupeUnavailable):
            await claim_webhook_update(42, {}, asyncio.Lock())
    assert redis.values[key] == stored


async def test_reservation_only_acknowledges_after_confirmed_queue_handoff(monkeypatch):
    redis = _RedisClaims()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    claim = await claim_webhook_update(42, {}, asyncio.Lock())
    assert claim is not None
    with pytest.raises(WebhookDedupeUnavailable):
        await claim_webhook_update(42, {}, asyncio.Lock())
    await claim.confirm_enqueued()
    assert await claim_webhook_update(42, {}, asyncio.Lock()) is None


async def test_reservation_release_cannot_remove_a_replacement_owner(monkeypatch):
    redis = _RedisClaims()
    monkeypatch.setattr("app.webhook_dedupe.redis_client", redis)
    claim = await claim_webhook_update(42, {}, asyncio.Lock())
    key = "telegram:webhook:update:42"
    redis.values[key] = "pending:replacement"
    await claim.release()
    assert redis.values[key] == "pending:replacement"
