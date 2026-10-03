import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.cache import MultiLayerCache


class TestCacheTTL(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Isolate from global redis_client which might fail without a server
        self.cache = MultiLayerCache()

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_ttl_cache_initialization(self):
        """Verify that TTLCache instances are correctly created with custom constraints."""
        from cachetools import TTLCache

        # Verify cache typing
        self.assertIsInstance(self.cache.qna_cache, TTLCache)
        self.assertIsInstance(self.cache.search_cache, TTLCache)
        self.assertIsInstance(self.cache.default_cache, TTLCache)

        # Verify sizes and TTL bounds specific to cache tier
        self.assertEqual(self.cache.qna_cache.maxsize, 500)
        self.assertEqual(self.cache.search_cache.maxsize, 500)
        self.assertEqual(self.cache.default_cache.maxsize, 200)

    @patch("app.cache.redis_client", None)  # Force bypass Redis
    def test_cache_set_and_get(self):
        """Verify memory cache effectively overrides dict mapping."""

        async def run_test():
            await self.cache.set("test_q", "qna", {"answer": "42"})

            # Since mock bypasses redis, get should fetch from memory
            result = await self.cache.get("test_q", "qna")

            self.assertIsNotNone(result)
            self.assertEqual(result["answer"], "42")

            # The item should exist directly within the underlying TTLCache map
            self.assertIn("test_q", self.cache.qna_cache)
            self.assertEqual(self.cache.qna_cache["test_q"]["answer"], "42")

        self.loop.run_until_complete(run_test())

    def test_get_memory_stats(self):
        """Verify TTLCache length combinations calculate correctly for monitoring."""
        # Add items to separate tiers
        self.cache.qna_cache["q1"] = 1
        self.cache.search_cache["s1"] = 1
        self.cache.default_cache["d1"] = 1

        stats = self.cache.get_memory_stats()

        # Total items = 3
        self.assertEqual(stats["memory_items"], 3)
        # Total max size = 500 + 500 + 200 = 1200
        self.assertEqual(stats["memory_max_size"], 1200)

        # Utilization = 3 / 1200 * 100 = 0.25
        self.assertEqual(stats["memory_utilization"], 0.25)

    def test_clear_cache(self):
        """The public cache-clear operation flushes the configured Redis store."""
        from app.cache import clear_cache

        redis = AsyncMock()
        redis.flushdb.return_value = True
        with patch("app.cache.redis_client", redis):
            self.loop.run_until_complete(clear_cache())
        redis.flushdb.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
