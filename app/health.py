"""
Health check system for all bot dependencies.
Provides comprehensive health monitoring and status reporting.
"""

import asyncio
import logging
import time
from typing import Dict, Any
from dataclasses import dataclass

from app.cache import redis_client


@dataclass
class HealthStatus:
    """Health status data structure."""

    status: str  # "healthy", "degraded", "unhealthy"
    message: str
    details: Dict[str, Any]
    timestamp: float


async def check_redis_health() -> HealthStatus:
    """Checks Redis health with improved error handling for Upstash."""
    start_time = time.time()
    details = {}

    try:
        if not redis_client:
            return HealthStatus(
                status="degraded",
                message="Redis client not configured",
                details={"warning": "REDIS_URL environment variable not set"},
                timestamp=start_time,
            )

        # Quick connection test with timeout
        try:
            await asyncio.wait_for(asyncio.to_thread(redis_client.ping), timeout=3.0)
            details["ping"] = "success"

        except asyncio.TimeoutError:
            return HealthStatus(
                status="degraded",
                message="Redis connection timeout",
                details={
                    "ping": "timeout",
                    "warning": "Consider checking Upstash connection limits",
                },
                timestamp=start_time,
            )

        except Exception as e:
            return HealthStatus(
                status="degraded",
                message=f"Redis connection error: {str(e)}",
                details={
                    "ping": "failed",
                    "error": str(e),
                    "warning": "Upstash may have closed the connection",
                },
                timestamp=start_time,
            )

        # Get Redis info with timeout
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(redis_client.info), timeout=3.0
            )

            # Parse info response safely
            if isinstance(info, bytes):
                info_str = info.decode("utf-8")
            else:
                info_str = str(info)

            # Extract basic stats
            stats = {}
            for line in info_str.split("\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    stats[key.strip()] = value.strip()

            details.update(
                {
                    "info": "success",
                    "total_keys": stats.get("db0", "0"),
                    "used_memory": stats.get("used_memory_human", "N/A"),
                    "uptime_in_days": stats.get("uptime_in_days", "N/A"),
                    "connected_clients": stats.get("connected_clients", "N/A"),
                }
            )

        except asyncio.TimeoutError:
            details["info"] = "timeout"
            details["warning"] = "Redis info command timed out"

        except Exception as e:
            details["info"] = "failed"
            details["error"] = str(e)

        # Determine overall status
        if details.get("ping") == "success" and details.get("info") == "success":
            status = "healthy"
            message = "Redis is operational"
        elif details.get("ping") == "success":
            status = "degraded"
            message = "Redis is connected but info unavailable"
        else:
            status = "degraded"
            message = "Redis has connection issues"

        return HealthStatus(
            status=status, message=message, details=details, timestamp=start_time
        )

    except Exception as e:
        logging.error(f"Unexpected error in Redis health check: {e}")
        return HealthStatus(
            status="unhealthy",
            message=f"Redis health check failed: {str(e)}",
            details={"error": str(e)},
            timestamp=start_time,
        )


async def get_system_health() -> Dict[str, Any]:
    """Gets overall system health including Redis."""
    health_data = {"timestamp": time.time(), "status": "healthy", "services": {}}

    # Check Redis health
    redis_health = await check_redis_health()
    health_data["services"]["redis"] = {
        "status": redis_health.status,
        "message": redis_health.message,
        "details": redis_health.details,
    }

    # Determine overall status
    if redis_health.status == "unhealthy":
        health_data["status"] = "degraded"
    elif redis_health.status == "degraded" and health_data["status"] == "healthy":
        health_data["status"] = "degraded"

    return health_data
