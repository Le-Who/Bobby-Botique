import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.webhook_dedupe import should_accept_webhook_update


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
