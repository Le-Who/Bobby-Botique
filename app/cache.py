import hashlib
import json
import logging
import os
import asyncio
import time
from typing import Dict, Any, Optional, Union
from functools import lru_cache
import threading

from redis import Redis

from app.metrics import metrics_collector
from app.exceptions import RedisConnectionError, CacheKeyError

# Initialize Redis client from environment variable with Upstash.com optimizations
redis_url = os.getenv("REDIS_URL")
if not redis_url:
    logging.warning("REDIS_URL environment variable not set. Caching will be disabled.")
    redis_client = None
else:
    try:
        # Configure Redis client for Upstash.com free tier with better error handling
        # Using minimal configuration to avoid compatibility issues
        redis_client = Redis.from_url(
            redis_url,
            socket_timeout=10,  # Increased timeout for better reliability
            socket_connect_timeout=10,  # Increased connect timeout
            max_connections=1,  # Single connection for stability
            retry_on_timeout=True
        )
        # Don't test connection during initialization to avoid blocking
        logging.info("Redis client initialized successfully for Upstash.com")
    except Exception as e:
        logging.warning("Failed to connect to Redis: %s. Caching will be disabled.", e)
        redis_client = None

# Thread-safe cache lock
_cache_lock = threading.Lock()

def _generate_cache_key(query: str, search_type: str) -> str:
    """Generates a cache key for the request."""
    normalized_query = " ".join(query.lower().split())
    key_data = f"{search_type}:{normalized_query}"
    return hashlib.sha256(key_data.encode()).hexdigest()

def _get_ttl(search_type: str) -> int:
    """Returns the TTL for the search type."""
    if search_type == 'qna':
        return 7200  # 2 hours
    elif search_type == 'search':
        return 1800  # 30 minutes
    else:
        return 3600  # 1 hour

async def get_cached_search_result(query: str, search_type: str) -> Optional[Dict[str, Any]]:
    """Gets the search result from the cache (Redis first, then memory fallback)."""
    # Сначала пробуем multi-layer cache
    try:
        result = await get_cached_search_result_ml(query, search_type)
        if result:
            return result
    except Exception as e:
        logging.warning("Multi-layer cache error, falling back to Redis: %s", e)
    
    # Fallback к старому Redis-only подходу
    if not redis_client:
        await metrics_collector.record_cache_miss()
        return None
    
    cache_key = _generate_cache_key(query, search_type)
    try:
        # Используем asyncio.to_thread для синхронных Redis операций
        cached_data = await asyncio.to_thread(redis_client.get, cache_key)
        if cached_data:
            await metrics_collector.record_cache_hit()
            logging.info("Cache hit for query: %s...", query[:50])
            # Handle both bytes and string responses
            if isinstance(cached_data, bytes):
                return json.loads(cached_data.decode('utf-8'))
            else:
                return json.loads(cached_data)
        else:
            await metrics_collector.record_cache_miss()
            return None
    except Exception as e:
        logging.error("Error getting from Redis cache: %s", e)
        await metrics_collector.record_cache_miss()
        return None

async def cache_search_result(query: str, search_type: str, result: Dict[str, Any]):
    """Saves the search result to the cache (multi-layer preferred)."""
    # Сначала пробуем multi-layer cache
    try:
        await cache_search_result_ml(query, search_type, result)
        return
    except Exception as e:
        logging.warning("Multi-layer cache save error, falling back to Redis: %s", e)
    
    # Fallback к старому Redis-only подходу
    if not redis_client:
        return

    cache_key = _generate_cache_key(query, search_type)
    ttl = _get_ttl(search_type)
    try:
        # Используем asyncio.to_thread для синхронных Redis операций
        await asyncio.to_thread(redis_client.setex, cache_key, ttl, json.dumps(result))
        logging.info(f"Cached search result for query: {query[:50]}...")
    except Exception as e:
        logging.error(f"Error caching result to Redis: {e}")

async def get_cache_stats() -> Dict[str, Any]:
    """Returns cache statistics."""
    if not redis_client:
        return {"error": "Redis client not configured"}
    
    try:
        # Используем asyncio.to_thread для синхронных Redis операций
        info = await asyncio.to_thread(redis_client.info)
        return {
            'total_keys': info.get('db0', {}).get('keys', 0),
            'used_memory': info.get('used_memory_human', 'N/A'),
            'uptime_in_days': info.get('uptime_in_days', 'N/A'),
            'cache_hit_rate': await metrics_collector.get_cache_hit_rate()
        }
    except Exception as e:
        logging.error(f"Error getting Redis stats: {e}")
        return {"error": str(e)}

async def clear_cache():
    """Clears the entire cache."""
    if not redis_client:
        return
        
    try:
        # Используем asyncio.to_thread для синхронных Redis операций
        await asyncio.to_thread(redis_client.flushdb)
        logging.info("Cache cleared")
    except Exception as e:
        logging.error("Error clearing Redis cache: %s", e)


# Multi-layer caching implementation
class MultiLayerCache:
    """Multi-layer caching system: Memory -> Redis -> Database"""
    
    def __init__(self):
        self.memory_cache = {}
        self.memory_cache_ttl = {}
        self.memory_cache_max_size = 1000  # Maximum items in memory cache
        
    def _cleanup_memory_cache(self):
        """Cleans up expired items from memory cache"""
        current_time = time.time()
        expired_keys = [
            key for key, expiry in self.memory_cache_ttl.items() 
            if current_time > expiry
        ]
        for key in expired_keys:
            del self.memory_cache[key]
            del self.memory_cache_ttl[key]
        
        # If still too many items, remove oldest
        if len(self.memory_cache) > self.memory_cache_max_size:
            items_to_remove = len(self.memory_cache) - self.memory_cache_max_size
            oldest_keys = sorted(self.memory_cache_ttl.items(), key=lambda x: x[1])[:items_to_remove]
            for key, _ in oldest_keys:
                del self.memory_cache[key]
                del self.memory_cache_ttl[key]
    
    async def get(self, key: str, search_type: str) -> Optional[Dict[str, Any]]:
        """Gets value from multi-layer cache"""
        # Try memory cache first
        if key in self.memory_cache:
            if time.time() < self.memory_cache_ttl.get(key, 0):
                logging.info("Memory cache hit for key: %s", key)
                return self.memory_cache[key]
            else:
                # Expired, remove from memory
                del self.memory_cache[key]
                del self.memory_cache_ttl[key]
        
        # Try Redis cache
        if redis_client:
            try:
                redis_key = f"{search_type}:{key}"
                # Используем asyncio.to_thread для синхронных Redis операций
                cached_data = await asyncio.to_thread(redis_client.get, redis_key)
                if cached_data:
                    # Handle both bytes and string responses
                    if isinstance(cached_data, bytes):
                        result = json.loads(cached_data.decode('utf-8'))
                    else:
                        result = json.loads(cached_data)
                    
                    # Store in memory cache for faster access
                    ttl = _get_ttl(search_type)
                    self.memory_cache[key] = result
                    self.memory_cache_ttl[key] = time.time() + ttl
                    self._cleanup_memory_cache()
                    
                    await metrics_collector.record_cache_hit()
                    logging.info("Redis cache hit for key: %s", key)
                    return result
            except Exception as e:
                logging.warning("Redis cache error: %s", e)
        
        await metrics_collector.record_cache_miss()
        return None
    
    async def set(self, key: str, search_type: str, value: Dict[str, Any]):
        """Sets value in multi-layer cache"""
        ttl = _get_ttl(search_type)
        
        # Store in memory cache
        self.memory_cache[key] = value
        self.memory_cache_ttl[key] = time.time() + ttl
        self._cleanup_memory_cache()
        
        # Store in Redis cache
        if redis_client:
            try:
                redis_key = f"{search_type}:{key}"
                # Используем asyncio.to_thread для синхронных Redis операций
                await asyncio.to_thread(redis_client.setex, redis_key, ttl, json.dumps(value))
                logging.info("Stored in Redis cache: %s", key)
            except Exception as e:
                logging.warning("Failed to store in Redis cache: %s", e)
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """Returns memory cache statistics"""
        self._cleanup_memory_cache()
        return {
            'memory_items': len(self.memory_cache),
            'memory_max_size': self.memory_cache_max_size,
            'memory_utilization': len(self.memory_cache) / self.memory_cache_max_size * 100
        }


# Global multi-layer cache instance
multi_layer_cache = MultiLayerCache()


async def get_cached_search_result_ml(query: str, search_type: str) -> Optional[Dict[str, Any]]:
    """Gets search result using multi-layer cache"""
    cache_key = _generate_cache_key(query, search_type)
    return await multi_layer_cache.get(cache_key, search_type)


async def cache_search_result_ml(query: str, search_type: str, result: Dict[str, Any]):
    """Saves search result using multi-layer cache"""
    cache_key = _generate_cache_key(query, search_type)
    await multi_layer_cache.set(cache_key, search_type, result)


async def get_multi_layer_cache_stats() -> Dict[str, Any]:
    """Returns multi-layer cache statistics"""
    redis_stats = await get_cache_stats()
    memory_stats = multi_layer_cache.get_memory_stats()
    
    return {
        'redis': redis_stats,
        'memory': memory_stats,
        'total_utilization': memory_stats['memory_utilization']
    }

