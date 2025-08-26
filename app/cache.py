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
from redis.exceptions import ConnectionError, TimeoutError, RedisError

from app.metrics import metrics_collector
from app.exceptions import RedisConnectionError, CacheKeyError

# Initialize Redis client with Upstash.com optimized configuration
redis_url = os.getenv("REDIS_URL")
if not redis_url:
    logging.warning("REDIS_URL environment variable not set. Caching will be disabled.")
    redis_client = None
else:
    try:
        # Minimal Redis configuration for Upstash.com free tier compatibility
        # Removed problematic parameters that cause connection issues
        redis_client = Redis.from_url(
            redis_url,
            socket_timeout=5,  # Fast timeout for quick failure detection
            socket_connect_timeout=5,  # Fast connect timeout
            max_connections=1,  # Single connection for stability
            retry_on_timeout=True,  # Only retry on timeout, not all errors
            decode_responses=False,  # Keep as bytes for manual handling
        )
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

def _safe_decode_redis_response(data: Union[bytes, str, None]) -> Optional[Dict[str, Any]]:
    """Safely decodes Redis response, handling both bytes and string responses."""
    if data is None:
        return None
    
    try:
        if isinstance(data, bytes):
            return json.loads(data.decode('utf-8'))
        elif isinstance(data, str):
            return json.loads(data)
        else:
            logging.warning(f"Unexpected Redis response type: {type(data)}")
            return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.error(f"Failed to decode Redis response: {e}")
        return None

async def _redis_operation_with_retry(operation, *args, max_retries=3, **kwargs):
    """Executes Redis operation with improved retry logic."""
    if not redis_client:
        raise RedisConnectionError("Redis client not configured")
    
    last_error = None
    for attempt in range(max_retries):
        try:
            # Check connection health before operation (starting from 2nd attempt)
            if attempt > 0:
                try:
                    await asyncio.to_thread(redis_client.ping)
                except Exception:
                    logging.warning(f"Redis connection check failed, attempt {attempt + 1}")
                    # Continue to retry even if ping fails
                    pass
            
            # Execute operation
            result = await asyncio.to_thread(operation, *args, **kwargs)
            return result
            
        except (ConnectionError, TimeoutError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 0.1  # Exponential backoff: 0.1s, 0.2s, 0.4s
                logging.warning(f"Redis operation failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"Redis operation failed after {max_retries} attempts: {e}")
                raise RedisConnectionError(f"Redis operation failed: {e}")
                
        except RedisError as e:
            # Other Redis errors don't require retry
            logging.error(f"Redis operation error: {e}")
            raise RedisConnectionError(f"Redis operation error: {e}")
    
    raise RedisConnectionError(f"Redis operation failed after {max_retries} attempts: {last_error}")

async def _redis_operation_with_retry_enhanced(operation, *args, max_retries=3, **kwargs):
    """Enhanced Redis operation with better error handling and connection management."""
    if not redis_client:
        raise RedisConnectionError("Redis client not configured")
    
    last_error = None
    for attempt in range(max_retries):
        try:
            # Check connection health before operation (starting from 2nd attempt)
            if attempt > 0:
                try:
                    await asyncio.to_thread(redis_client.ping)
                except Exception:
                    logging.warning(f"Redis connection check failed, attempt {attempt + 1}")
                    # Continue to retry even if ping fails
                    pass
            
            # Execute operation with timeout
            result = await asyncio.wait_for(
                asyncio.to_thread(operation, *args, **kwargs),
                timeout=5.0  # 5 second timeout for Redis operations
            )
            return result
            
        except asyncio.TimeoutError:
            last_error = "Operation timeout"
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 0.2  # Exponential backoff: 0.2s, 0.4s, 0.8s
                logging.warning(f"Redis operation timeout (attempt {attempt + 1}/{max_retries}). Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"Redis operation timeout after {max_retries} attempts")
                raise RedisConnectionError("Redis operation timeout")
                
        except (ConnectionError, TimeoutError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 0.2  # Exponential backoff: 0.2s, 0.4s, 0.8s
                logging.warning(f"Redis operation failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"Redis operation failed after {max_retries} attempts: {e}")
                raise RedisConnectionError(f"Redis operation failed: {e}")
                
        except RedisError as e:
            # Other Redis errors don't require retry
            logging.error(f"Redis operation error: {e}")
            raise RedisConnectionError(f"Redis operation error: {e}")
    
    raise RedisConnectionError(f"Redis operation failed after {max_retries} attempts: {last_error}")

async def get_cached_search_result(query: str, search_type: str) -> Optional[Dict[str, Any]]:
    """Gets the search result from the cache (multi-layer first, then Redis fallback)."""
    # Try multi-layer cache first
    try:
        result = await get_cached_search_result_ml(query, search_type)
        if result:
            return result
    except Exception as e:
        logging.warning("Multi-layer cache error, falling back to Redis: %s", e)
    
    # Fallback to Redis-only approach
    if not redis_client:
        await metrics_collector.record_cache_miss()
        return None
    
    cache_key = _generate_cache_key(query, search_type)
    try:
        # Use retry logic for Redis operations
        cached_data = await _redis_operation_with_retry(redis_client.get, cache_key)
        
        if cached_data:
            await metrics_collector.record_cache_hit()
            logging.info("Cache hit for query: %s...", query[:50])
            
            # Safely decode the response
            result = _safe_decode_redis_response(cached_data)
            if result:
                return result
            else:
                logging.warning("Failed to decode cached data for query: %s", query[:50])
                await metrics_collector.record_cache_miss()
                return None
        else:
            await metrics_collector.record_cache_miss()
            return None
            
    except RedisConnectionError as e:
        logging.warning(f"Redis cache unavailable: {e}")
        await metrics_collector.record_cache_miss()
        return None
    except Exception as e:
        logging.error("Error getting from Redis cache: %s", e)
        await metrics_collector.record_cache_miss()
        return None

async def cache_search_result(query: str, search_type: str, result: Dict[str, Any]):
    """Saves the search result to the cache (multi-layer preferred)."""
    # Try multi-layer cache first
    try:
        await cache_search_result_ml(query, search_type, result)
        return
    except Exception as e:
        logging.warning("Multi-layer cache save error, falling back to Redis: %s", e)
    
    # Fallback to Redis-only approach
    if not redis_client:
        return

    cache_key = _generate_cache_key(query, search_type)
    ttl = _get_ttl(search_type)
    
    try:
        # Serialize result to JSON string
        json_data = json.dumps(result, ensure_ascii=False)
        
        # Use retry logic for Redis operations
        await _redis_operation_with_retry(redis_client.setex, cache_key, ttl, json_data)
        logging.info(f"Cached search result for query: {query[:50]}...")
        
    except RedisConnectionError as e:
        logging.warning(f"Failed to store in Redis cache (connection issue): {e}")
    except Exception as e:
        logging.error(f"Error caching result to Redis: {e}")

async def get_cache_stats() -> Dict[str, Any]:
    """Returns cache statistics."""
    if not redis_client:
        return {"error": "Redis client not configured"}
    
    try:
        # Use retry logic for Redis operations
        info = await _redis_operation_with_retry(redis_client.info)
        
        # Parse info response safely
        if isinstance(info, bytes):
            info_str = info.decode('utf-8')
        else:
            info_str = str(info)
        
        # Extract basic stats
        stats = {}
        for line in info_str.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                stats[key.strip()] = value.strip()
        
        return {
            'total_keys': stats.get('db0', '0'),
            'used_memory': stats.get('used_memory_human', 'N/A'),
            'uptime_in_days': stats.get('uptime_in_days', 'N/A'),
            'cache_hit_rate': await metrics_collector.get_cache_hit_rate()
        }
        
    except RedisConnectionError as e:
        logging.warning(f"Redis stats unavailable: {e}")
        return {"error": f"Redis connection issue: {e}"}
    except Exception as e:
        logging.error(f"Error getting Redis stats: {e}")
        return {"error": str(e)}

async def clear_cache():
    """Clears the entire cache."""
    if not redis_client:
        return
        
    try:
        # Use retry logic for Redis operations
        await _redis_operation_with_retry(redis_client.flushdb)
        logging.info("Cache cleared")
    except RedisConnectionError as e:
        logging.warning(f"Failed to clear Redis cache (connection issue): {e}")
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
                # Use retry logic for Redis operations
                cached_data = await _redis_operation_with_retry(redis_client.get, redis_key)
                
                if cached_data:
                    # Safely decode the response
                    result = _safe_decode_redis_response(cached_data)
                    
                    if result:
                        # Store in memory cache for faster access
                        ttl = _get_ttl(search_type)
                        self.memory_cache[key] = result
                        self.memory_cache_ttl[key] = time.time() + ttl
                        self._cleanup_memory_cache()
                        
                        await metrics_collector.record_cache_hit()
                        logging.info("Redis cache hit for key: %s", key)
                        return result
                    else:
                        logging.warning("Failed to decode Redis data for key: %s", key)
                        
            except RedisConnectionError as e:
                logging.warning(f"Redis cache unavailable: {e}")
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
                json_data = json.dumps(value, ensure_ascii=False)
                
                # Use retry logic for Redis operations
                await _redis_operation_with_retry(redis_client.setex, redis_key, ttl, json_data)
                logging.info("Stored in Redis cache: %s", key)
                
            except RedisConnectionError as e:
                logging.warning(f"Failed to store in Redis cache (connection issue): {e}")
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

