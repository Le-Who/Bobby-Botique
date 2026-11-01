"""Transport uncertainty must not repeat state-changing Redis operations."""

import pytest
from redis.exceptions import TimeoutError


@pytest.mark.asyncio
async def test_redis_mutation_is_not_replayed_after_lost_response():
    from app.cache import create_redis_client

    client = create_redis_client("redis://localhost:6379/15")
    connection = client.connection_pool.connection_class(**client.connection_pool.connection_kwargs)
    writes = []

    async def sent_but_response_lost():
        writes.append("claimed")
        raise TimeoutError("simulated lost response")

    async def disconnect(*args):
        pass

    try:
        with pytest.raises(TimeoutError):
            await connection.retry.call_with_retry(sent_but_response_lost, disconnect)
        assert writes == ["claimed"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redis_ping_has_short_independent_deadline(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import cache

    cancelled = asyncio.Event()

    async def stuck_ping():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(cache, "redis_client", SimpleNamespace(ping=stuck_ping))
    async with asyncio.timeout(2.5):
        assert await cache.ping_safe() is False
    assert cancelled.is_set()
