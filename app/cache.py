import hashlib
import json
import logging
import os
from typing import Dict, Any, Optional

from redis import Redis

from .metrics import metrics_collector

# Initialize Redis client from environment variable with Upstash.com optimizations
redis_url = os.getenv("REDIS_URL")
if not redis_url:
    logging.warning("REDIS_URL environment variable not set. Caching will be disabled.")
    redis_client = None
else:
    try:
        # Configure Redis client for Upstash.com free tier
        redis_client = Redis.from_url(
            redis_url,
            socket_timeout=5,  # 5 seconds socket timeout
            socket_connect_timeout=5,  # 5 seconds connect timeout
            socket_keepalive=True,  # Keep connections alive
            socket_keepalive_options={},
            health_check_interval=30,  # Health check every 30 seconds
            max_connections=2,  # Limit connections for free tier
            retry_on_timeout=True
            # Removed ssl_cert_reqs as it's not supported in newer Redis versions
        )
        # Test connection
        redis_client.ping()
        logging.info("Redis client initialized successfully for Upstash.com")
    except Exception as e:
        logging.error(f"Failed to connect to Redis: {e}")
        redis_client = None

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
    """Gets the search result from the cache."""
    if not redis_client:
        return None
    
    cache_key = _generate_cache_key(query, search_type)
    try:
        cached_data = redis_client.get(cache_key)
        if cached_data:
            await metrics_collector.record_cache_hit()
            logging.info(f"Cache hit for query: {query[:50]}...")
            return json.loads(cached_data)
        else:
            await metrics_collector.record_cache_miss()
            return None
    except Exception as e:
        logging.error(f"Error getting from Redis cache: {e}")
        return None

async def cache_search_result(query: str, search_type: str, result: Dict[str, Any]):
    """Saves the search result to the cache."""
    if not redis_client:
        return

    cache_key = _generate_cache_key(query, search_type)
    ttl = _get_ttl(search_type)
    try:
        redis_client.setex(cache_key, ttl, json.dumps(result))
        logging.info(f"Cached search result for query: {query[:50]}...")
    except Exception as e:
        logging.error(f"Error caching result to Redis: {e}")

async def get_cache_stats() -> Dict[str, Any]:
    """Returns cache statistics."""
    if not redis_client:
        return {"error": "Redis client not configured"}
    
    try:
        info = redis_client.info()
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
        redis_client.flushdb()
        logging.info("Cache cleared")
    except Exception as e:
        logging.error(f"Error clearing Redis cache: {e}")

