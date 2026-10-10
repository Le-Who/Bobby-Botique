import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.webhook_dedupe import should_accept_webhook_update


class _RedisClaims:
    def __init__(self):
        self.claims = set()
        self.available = True
        self.cancel_at = None

    async def set(self, key, value, *, ex, nx):
        assert value == "1" and ex > 0 and nx is True
        if not self.available:
            raise ConnectionError("synthetic Redis outage")
        if key in self.claims:
            return None
        self.claims.add(key)
        if len(self.claims) == self.cancel_at:
            raise asyncio.CancelledError
        return True


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
    assert await should_accept_webhook_update(replay_id, seen, lock, payload=payload, ttl_seconds=4)
    assert not await should_accept_webhook_update(replay_id, seen, lock, payload=payload, ttl_seconds=4)


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
    assert await should_accept_webhook_update(10 if command else 1, seen, lock, payload=evicted, capacity=4)
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
    assert await should_accept_webhook_update(2, seen, lock)
    assert not await should_accept_webhook_update(2, seen, lock)
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
    assert "telegram:webhook:update:1" in redis.claims


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
    redis = AsyncMock()
    redis.set = AsyncMock(side_effect=[True, None])

    with patch("app.webhook_dedupe.redis_client", redis):
        assert await should_accept_webhook_update(42, {}, asyncio.Lock())
        assert not await should_accept_webhook_update(42, {}, asyncio.Lock())

    redis.set.assert_any_await("telegram:webhook:update:42", "1", ex=180, nx=True)


@pytest.mark.asyncio
async def test_webhook_update_dedupe_falls_back_to_local_ttl_cache() -> None:
    seen: dict[int, float] = {}
    lock = asyncio.Lock()

    with patch("app.webhook_dedupe.redis_client", None):
        assert await should_accept_webhook_update(77, seen, lock)
        assert not await should_accept_webhook_update(77, seen, lock)


@pytest.mark.asyncio
async def test_webhook_command_dedupe_rejects_same_message_with_new_update_id() -> None:
    redis = AsyncMock()

    async def fake_set(key, *_args, **_kwargs):
        return not key.endswith("cmd:private:123:55:start")

    redis.set = AsyncMock(side_effect=fake_set)
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
