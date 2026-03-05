"""Tests for degradation matrix — graceful degradation and recovery scenarios."""

import pytest

from app.degradation import ServiceStatus, SystemHealth, can_process_message


class TestSystemHealthOverall:
    """Test the overall health computation from component statuses."""

    def test_all_healthy(self):
        health = SystemHealth()
        assert health.overall == ServiceStatus.HEALTHY

    def test_single_degraded_component(self):
        health = SystemHealth(redis=ServiceStatus.DEGRADED)
        assert health.overall == ServiceStatus.DEGRADED

    def test_single_unavailable_component_degrades_overall(self):
        health = SystemHealth(ai_provider=ServiceStatus.UNAVAILABLE)
        assert health.overall == ServiceStatus.DEGRADED

    def test_all_unavailable(self):
        health = SystemHealth(
            database=ServiceStatus.UNAVAILABLE,
            redis=ServiceStatus.UNAVAILABLE,
            ai_provider=ServiceStatus.UNAVAILABLE,
        )
        assert health.overall == ServiceStatus.DEGRADED

    def test_mixed_statuses(self):
        health = SystemHealth(
            database=ServiceStatus.HEALTHY,
            redis=ServiceStatus.DEGRADED,
            ai_provider=ServiceStatus.HEALTHY,
        )
        assert health.overall == ServiceStatus.DEGRADED


class TestCanProcessMessage:
    """Test message processing decisions under different health conditions."""

    def test_healthy_system_allows_messages(self):
        health = SystemHealth()
        can, msg = can_process_message(health)
        assert can is True
        assert msg is None

    def test_database_unavailable_blocks_messages(self):
        health = SystemHealth(database=ServiceStatus.UNAVAILABLE)
        can, msg = can_process_message(health)
        assert can is False
        assert msg is not None
        assert "базы данных" in msg

    def test_ai_provider_unavailable_blocks_messages(self):
        health = SystemHealth(ai_provider=ServiceStatus.UNAVAILABLE)
        can, msg = can_process_message(health)
        assert can is False
        assert "AI" in msg

    def test_redis_unavailable_still_allows_messages(self):
        """Redis down should NOT block message processing — it's a cache, not critical."""
        health = SystemHealth(redis=ServiceStatus.UNAVAILABLE)
        can, msg = can_process_message(health)
        assert can is True
        assert msg is None

    def test_redis_degraded_allows_messages(self):
        health = SystemHealth(redis=ServiceStatus.DEGRADED)
        can, msg = can_process_message(health)
        assert can is True

    def test_database_degraded_allows_messages(self):
        """Degraded is not unavailable — should still process."""
        health = SystemHealth(database=ServiceStatus.DEGRADED)
        can, msg = can_process_message(health)
        assert can is True


class TestSystemHealthRecovery:
    """Test that health correctly reflects recovery from degraded states."""

    def test_recovery_from_degraded_to_healthy(self):
        """Simulates a service recovering: health snapshot reflects new state."""
        # During outage
        unhealthy = SystemHealth(database=ServiceStatus.UNAVAILABLE)
        assert unhealthy.overall == ServiceStatus.DEGRADED
        can, _ = can_process_message(unhealthy)
        assert can is False

        # After recovery — new snapshot
        recovered = SystemHealth(database=ServiceStatus.HEALTHY)
        assert recovered.overall == ServiceStatus.HEALTHY
        can, _ = can_process_message(recovered)
        assert can is True

    def test_partial_recovery(self):
        """One component recovers but another stays down."""
        health = SystemHealth(
            database=ServiceStatus.HEALTHY,
            ai_provider=ServiceStatus.UNAVAILABLE,
        )
        can, msg = can_process_message(health)
        assert can is False
        assert "AI" in msg


class TestToDict:
    """Test serialization for health endpoint responses."""

    def test_to_dict_all_healthy(self):
        d = SystemHealth().to_dict()
        assert d["overall"] == "healthy"
        assert d["database"] == "healthy"
        assert d["redis"] == "healthy"
        assert d["ai_provider"] == "healthy"
        assert isinstance(d["details"], dict)

    def test_to_dict_degraded(self):
        d = SystemHealth(redis=ServiceStatus.UNAVAILABLE).to_dict()
        assert d["overall"] == "degraded"
        assert d["redis"] == "unavailable"

    def test_to_dict_includes_details(self):
        d = SystemHealth(details={"latency_ms": 1500}).to_dict()
        assert d["details"]["latency_ms"] == 1500
