# /app/degradation.py
"""Degradation matrix — graceful fallbacks when services are unavailable.

Provides health checks and fallback strategies for:
- Database unavailable → cache-only mode
- Redis unavailable → in-memory fallback (already handled by cache.py)
- AI provider unavailable → user notification + retry
"""

import logging
from dataclasses import dataclass, field
from enum import Enum


class ServiceStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class SystemHealth:
    """Snapshot of all service health statuses."""
    database: ServiceStatus = ServiceStatus.HEALTHY
    redis: ServiceStatus = ServiceStatus.HEALTHY
    ai_provider: ServiceStatus = ServiceStatus.HEALTHY
    details: dict = field(default_factory=dict)

    @property
    def overall(self) -> ServiceStatus:
        statuses = [self.database, self.redis, self.ai_provider]
        if ServiceStatus.UNAVAILABLE in statuses:
            return ServiceStatus.DEGRADED
        if ServiceStatus.DEGRADED in statuses:
            return ServiceStatus.DEGRADED
        return ServiceStatus.HEALTHY

    def to_dict(self) -> dict:
        return {
            "overall": self.overall.value,
            "database": self.database.value,
            "redis": self.redis.value,
            "ai_provider": self.ai_provider.value,
            "details": self.details,
        }


async def check_system_health() -> SystemHealth:
    """Run all health checks and return system-wide status."""
    health = SystemHealth()

    # ── Database ─────────────────────────────────────────────────────────
    try:
        from app.database import check_database_health
        db_ok = await check_database_health()
        health.database = ServiceStatus.HEALTHY if db_ok else ServiceStatus.UNAVAILABLE
    except Exception as e:
        health.database = ServiceStatus.UNAVAILABLE
        health.details["database_error"] = str(e)

    # ── Redis ────────────────────────────────────────────────────────────
    try:
        from app.cache import ping_safe, redis_client
        if redis_client is None:
            health.redis = ServiceStatus.DEGRADED
            health.details["redis"] = "not_configured"
        elif await ping_safe():
            health.redis = ServiceStatus.HEALTHY
        else:
            health.redis = ServiceStatus.UNAVAILABLE
    except Exception as e:
        health.redis = ServiceStatus.DEGRADED
        health.details["redis_error"] = str(e)

    # ── AI Provider ──────────────────────────────────────────────────────
    try:
        from app.circuit_breaker import CircuitState, _circuit_breakers
        ai_circuits = {
            name: cb for name, cb in _circuit_breakers.items()
            if "ai_provider" in name
        }
        open_count = sum(
            1 for cb in ai_circuits.values()
            if getattr(cb, "_state", None) == CircuitState.OPEN
        )
        if open_count > 0:
            health.ai_provider = ServiceStatus.DEGRADED
            health.details["ai_circuits_open"] = open_count
    except Exception:
        pass  # No circuit breakers = assume healthy

    return health


def can_process_message(health: SystemHealth) -> tuple[bool, str | None]:
    """Determine if we can process a user message given current health.

    Returns:
        (can_process, user_message) — if can_process is False, user_message
        contains the error to send to the user.
    """
    if health.database == ServiceStatus.UNAVAILABLE:
        return False, (
            "⚠️ Сервис базы данных временно недоступен. "
            "Ваше сообщение не может быть обработано. "
            "Попробуйте через несколько минут."
        )

    if health.ai_provider == ServiceStatus.UNAVAILABLE:
        return False, (
            "⚠️ AI-сервис временно недоступен. "
            "Попробуйте через несколько минут."
        )

    return True, None
