"""Tests for MultiLayerCache graceful degradation when Redis is unavailable."""

from unittest.mock import AsyncMock, patch

import pytest

from app.cache import MultiLayerCache


@pytest.fixture
def cache():
    """Fresh MultiLayerCache instance."""
    return MultiLayerCache()


@pytest.fixture(autouse=True)
def _patch_metrics():
    """Suppress metrics calls during cache tests."""
    with patch("app.cache.metrics_collector") as mock_mc:
        mock_mc.record_cache_hit = AsyncMock()
        mock_mc.record_cache_miss = AsyncMock()
        yield


class TestCacheRedisUnavailable:
    """When Redis is down, cache should fall back to memory-only mode."""

    @pytest.mark.asyncio
    async def test_get_returns_none_when_redis_disconnected(self, cache):
        """With no memory hit and Redis unavailable, get() returns None (not raises)."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        with patch("app.cache.redis_client") as mock_redis:
            mock_redis.get = AsyncMock(side_effect=RedisConnectionError("Connection refused"))

            result = await cache.get("missing-key", "qna")

        assert result is None

    @pytest.mark.asyncio
    async def test_set_stores_in_memory_when_redis_down(self, cache):
        """set() should still store in memory even if Redis write fails."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        data = {"answer": "42"}
        with patch("app.cache.redis_client") as mock_redis:
            mock_redis.setex = AsyncMock(side_effect=RedisConnectionError("Connection refused"))

            await cache.set("key1", "qna", data)

        # Memory cache should have it
        result = await cache.get("key1", "qna")
        assert result == data

    @pytest.mark.asyncio
    async def test_memory_hit_bypasses_redis_entirely(self, cache):
        """If value is in memory cache, Redis is never consulted."""
        data = {"answer": "cached"}
        # Manually populate memory cache
        cache.qna_cache["test-key"] = data

        with patch("app.cache.redis_client") as mock_redis:
            mock_redis.get = AsyncMock()

            result = await cache.get("test-key", "qna")

        assert result == data
        mock_redis.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redis_none_client_handled_gracefully(self, cache):
        """If redis_client is None (not configured), cache should work memory-only."""
        with patch("app.cache.redis_client", None):
            # Set in memory-only mode
            await cache.set("k1", "search", {"data": "value"})
            result = await cache.get("k1", "search")

        assert result == {"data": "value"}


class TestCacheMultiLayer:
    """Test the multi-layer (memory → Redis) lookup behavior."""

    @pytest.mark.asyncio
    async def test_redis_hit_populates_memory_cache(self, cache):
        """A Redis hit should also store the value in memory for next access."""
        import json

        data = {"result": "from_redis"}

        with patch("app.cache.redis_client") as mock_redis:
            mock_redis.get = AsyncMock(return_value=json.dumps(data))

            result = await cache.get("redis-key", "qna")

        assert result == data
        # Now memory cache should have it
        assert "redis-key" in cache.qna_cache

    @pytest.mark.asyncio
    async def test_search_type_routes_to_correct_cache(self, cache):
        """Different search types use different TTLCache instances."""
        cache.qna_cache["key"] = {"type": "qna"}
        cache.search_cache["key"] = {"type": "search"}

        with patch("app.cache.redis_client", None):
            qna_result = await cache.get("key", "qna")
            search_result = await cache.get("key", "search")

        assert qna_result["type"] == "qna"
        assert search_result["type"] == "search"
