import asyncio
import hashlib
import json
import logging
import os
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from app.errors import RedisConnectionError
from app.metrics import metrics_collector

# Initialize Redis client with Upstash.com optimized configuration
redis_url = os.getenv("REDIS_URL")
if not redis_url:
    logging.warning("REDIS_URL environment variable not set. Caching will be disabled.")
    redis_client = None
else:
    try:
        # Redis configuration for Upstash.com
        # Upstash free tier allows 100 concurrent connections.
        # max_connections=10 allows concurrent handlers (bot uses concurrent_updates=True)
        # while staying well within Upstash limits.
        redis_client = Redis.from_url(
            redis_url,
            socket_timeout=5,  # Fast timeout for quick failure detection
            socket_connect_timeout=5,  # Fast connect timeout
            max_connections=10,  # Match concurrent handler capacity
            retry_on_timeout=True,  # Only retry on timeout, not all errors
            decode_responses=False,  # Keep as bytes for manual handling
            health_check_interval=0,  # Disable built-in health-check pings
        )
        logging.info("Redis client initialized successfully for Upstash.com")
    except (ConnectionError, RedisError) as e:
        logging.warning("Failed to connect to Redis: %s. Caching will be disabled.", e)
        redis_client = None



async def ping_safe() -> bool:
    """Lightweight Redis ping for health checks. Returns bool, never throws."""
    if not redis_client:
        return False
    try:
        return bool(await redis_client.ping())
    except Exception:
        return False


async def shutdown_redis() -> None:
    """Close the async Redis client connection pool during graceful shutdown."""
    global redis_client
    if redis_client:
        try:
            await redis_client.aclose()
            logging.info("Redis client closed successfully")
        except Exception as e:
            logging.warning("Error closing Redis client: %s", e)
        finally:
            redis_client = None


def _generate_cache_key(query: str, search_type: str) -> str:
    """Generates a cache key for the request."""
    normalized_query = " ".join(query.lower().split())
    key_data = f"{search_type}:{normalized_query}"
    return hashlib.sha256(key_data.encode()).hexdigest()


def _get_ttl(search_type: str) -> int:
    """Returns the TTL for the search type."""
    if search_type == "qna":
        return 7200  # 2 hours
    elif search_type == "search":
        return 1800  # 30 minutes
    else:
        return 3600  # 1 hour


def _safe_decode_redis_response(
    data: bytes | str | None,
) -> dict[str, Any] | None:
    """Safely decodes Redis response, handling both bytes and string responses."""
    if data is None:
        return None

    try:
        if isinstance(data, bytes):
            return json.loads(data.decode("utf-8"))
        elif isinstance(data, str):
            return json.loads(data)
        else:
            logging.warning("Unexpected Redis response type: %s", type(data))
            return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.error("Failed to decode Redis response: %s", e, exc_info=True)
        return None


async def _redis_operation_with_retry(operation, *args, max_retries=3, **kwargs):
    """Executes async Redis operation with improved retry logic."""
    if not redis_client:
        raise RedisConnectionError("Redis client not configured")

    last_error = None
    for attempt in range(max_retries):
        try:
            # Execute async Redis operation directly
            result = await operation(*args, **kwargs)
            return result

        except (ConnectionError, TimeoutError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = (2**attempt) * 0.1  # Exponential backoff: 0.1s, 0.2s, 0.4s
                logging.warning(
                    f"Redis operation failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s..."
                )
                await asyncio.sleep(wait_time)
            else:
                logging.error(
                    f"Redis operation failed after {max_retries} attempts: {e}"
                )
                raise RedisConnectionError(f"Redis operation failed: {e}") from e

        except RedisError as e:
            # Other Redis errors don't require retry
            logging.error("Redis operation error: %s", e, exc_info=True)
            raise RedisConnectionError(f"Redis operation error: {e}") from e

    raise RedisConnectionError(
        f"Redis operation failed after {max_retries} attempts: {last_error}"
    )



async def get_cached_search_result(
    query: str, search_type: str
) -> dict[str, Any] | None:
    """Gets the search result from the multi-layer cache (Memory -> Redis)."""
    try:
        return await get_cached_search_result_ml(query, search_type)
    except Exception as e:
        logging.warning("Cache get error for query %s...: %s", query[:50], e)
        await metrics_collector.record_cache_miss()
        return None


async def cache_search_result(query: str, search_type: str, result: dict[str, Any]) -> None:
    """Saves the search result to the multi-layer cache (Memory + Redis)."""
    try:
        await cache_search_result_ml(query, search_type, result)
    except Exception as e:
        logging.warning("Cache set error for query %s...: %s", query[:50], e)


async def get_cache_stats() -> dict[str, Any]:
    """Returns cache statistics."""
    if not redis_client:
        return {"error": "Redis client not configured"}

    try:
        # Use retry logic for Redis operations
        info = await _redis_operation_with_retry(redis_client.info)

        # redis-py .info() returns a dict — access keys directly
        db0 = info.get("db0", {})
        total_keys = db0.get("keys", 0) if isinstance(db0, dict) else str(db0)

        return {
            "total_keys": total_keys,
            "used_memory": info.get("used_memory_human", "N/A"),
            "uptime_in_days": info.get("uptime_in_days", "N/A"),
            "connected_clients": info.get("connected_clients", "N/A"),
            "cache_hit_rate": metrics_collector.get_cache_hit_rate(),
        }

    except RedisConnectionError as e:
        logging.warning("Redis stats unavailable: %s", e)
        return {"error": f"Redis connection issue: {e}"}
    except RedisError as e:
        logging.error("Error getting Redis stats: %s", e, exc_info=True)
        return {"error": str(e)}


async def clear_cache() -> None:
    """Clears the entire cache."""
    if not redis_client:
        return

    try:
        # Use retry logic for Redis operations
        await _redis_operation_with_retry(redis_client.flushdb)
        logging.info("Cache cleared")
    except RedisConnectionError as e:
        logging.warning("Failed to clear Redis cache (connection issue): %s", e)
    except RedisError as e:
        logging.error("Error clearing Redis cache: %s", e, exc_info=True)


# Multi-layer caching implementation
class MultiLayerCache:
    """Multi-layer caching system: Memory -> Redis -> Database"""

    def __init__(self):
        from cachetools import TTLCache

        # Separate TTLCaches for exact expiration tracking based on search type constraints
        self.qna_cache = TTLCache(maxsize=500, ttl=7200)
        self.search_cache = TTLCache(maxsize=500, ttl=1800)
        self.default_cache = TTLCache(maxsize=200, ttl=3600)
        # asyncio.Lock to protect in-memory caches from concurrent coroutine access
        self._lock = asyncio.Lock()

    def _get_cache(self, search_type: str):
        if search_type == "qna":
            return self.qna_cache
        elif search_type == "search":
            return self.search_cache
        else:
            return self.default_cache


    async def get(self, key: str, search_type: str) -> dict[str, Any] | None:
        """Gets value from multi-layer cache"""
        cache_dict = self._get_cache(search_type)
        # Try memory cache first (under lock for TTLCache safety)
        async with self._lock:
            if key in cache_dict:
                logging.info("Memory cache hit for key: %s", key)
                return cache_dict[key]

        # Try Redis cache
        if redis_client:
            try:
                redis_key = f"{search_type}:{key}"
                # Use retry logic for Redis operations
                cached_data = await _redis_operation_with_retry(
                    redis_client.get, redis_key
                )

                if cached_data:
                    # Safely decode the response
                    result = _safe_decode_redis_response(cached_data)

                    if result:
                        # Store in memory cache for faster access
                        async with self._lock:
                            cache_dict[key] = result

                        await metrics_collector.record_cache_hit()
                        logging.info("Redis cache hit for key: %s", key)
                        return result
                    else:
                        logging.warning("Failed to decode Redis data for key: %s", key)

            except RedisConnectionError as e:
                logging.warning("Redis cache unavailable: %s", e)
            except RedisError as e:
                logging.warning("Redis cache error: %s", e)

        await metrics_collector.record_cache_miss()
        return None

    async def set(self, key: str, search_type: str, value: dict[str, Any]):
        """Sets value in multi-layer cache"""
        ttl = _get_ttl(search_type)

        # Store in memory cache
        cache_dict = self._get_cache(search_type)
        async with self._lock:
            cache_dict[key] = value

        # Store in Redis cache
        if redis_client:
            try:
                redis_key = f"{search_type}:{key}"
                json_data = json.dumps(value, ensure_ascii=False)

                # Use retry logic for Redis operations
                await _redis_operation_with_retry(
                    redis_client.setex, redis_key, ttl, json_data
                )
                logging.info("Stored in Redis cache: %s", key)

            except RedisConnectionError as e:
                logging.warning(
                    f"Failed to store in Redis cache (connection issue): {e}"
                )
            except RedisError as e:
                logging.warning("Failed to store in Redis cache: %s", e)

    def get_memory_stats(self) -> dict[str, Any]:
        """Returns memory cache statistics"""
        memory_items = (
            len(self.qna_cache) + len(self.search_cache) + len(self.default_cache)
        )
        memory_max_size = (
            self.qna_cache.maxsize
            + self.search_cache.maxsize
            + self.default_cache.maxsize
        )
        return {
            "memory_items": memory_items,
            "memory_max_size": memory_max_size,
            "memory_utilization": memory_items / memory_max_size * 100
            if memory_max_size
            else 0,
        }


# Global multi-layer cache instance
multi_layer_cache = MultiLayerCache()


async def get_cached_search_result_ml(
    query: str, search_type: str
) -> dict[str, Any] | None:
    """Gets search result using multi-layer cache"""
    cache_key = _generate_cache_key(query, search_type)
    return await multi_layer_cache.get(cache_key, search_type)


async def cache_search_result_ml(query: str, search_type: str, result: dict[str, Any]):
    """Saves search result using multi-layer cache"""
    cache_key = _generate_cache_key(query, search_type)
    await multi_layer_cache.set(cache_key, search_type, result)


async def get_multi_layer_cache_stats() -> dict[str, Any]:
    """Returns multi-layer cache statistics"""
    redis_stats = await get_cache_stats()
    memory_stats = multi_layer_cache.get_memory_stats()

    return {
        "redis": redis_stats,
        "memory": memory_stats,
        "total_utilization": memory_stats["memory_utilization"],
    }
