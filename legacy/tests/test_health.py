
import pytest
import asyncio
from unittest.mock import MagicMock, patch
import app.health

@pytest.mark.asyncio
async def test_check_redis_health_not_configured():
    """Test that health check returns degraded status when Redis is not configured."""
    with patch('app.health.redis_client', None):
        status = await app.health.check_redis_health()

        assert status.status == "degraded"
        assert status.message == "Redis client not configured"
        assert "warning" in status.details
        assert status.details["warning"] == "REDIS_URL environment variable not set"

@pytest.mark.asyncio
async def test_check_redis_health_success():
    """Test that health check returns healthy status when Redis is working correctly."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    mock_info_str = "db0:keys=10,expires=0,avg_ttl=0\nused_memory_human:1.5M\nuptime_in_days:5\nconnected_clients:2"
    mock_redis.info.return_value = mock_info_str.encode('utf-8')

    with patch('app.health.redis_client', mock_redis):
        status = await app.health.check_redis_health()

        assert status.status == "healthy"
        assert status.message == "Redis is operational"
        assert status.details["ping"] == "success"
        assert status.details["info"] == "success"
        assert status.details["total_keys"] == "keys=10,expires=0,avg_ttl=0"
        assert status.details["used_memory"] == "1.5M"
        assert status.details["uptime_in_days"] == "5"
        assert status.details["connected_clients"] == "2"

@pytest.mark.asyncio
async def test_check_redis_health_ping_timeout():
    """Test handling of ping timeout."""
    mock_redis = MagicMock()

    def side_effect(coro, timeout=None):
        coro.close()
        raise asyncio.TimeoutError("Timeout")

    with patch('app.health.redis_client', mock_redis):
        # Patch asyncio.wait_for to raise TimeoutError
        with patch('asyncio.wait_for', side_effect=side_effect):
             status = await app.health.check_redis_health()

             assert status.status == "degraded"
             assert status.message == "Redis connection timeout"
             assert status.details["ping"] == "timeout"

@pytest.mark.asyncio
async def test_check_redis_health_ping_error():
    """Test handling of ping error."""
    mock_redis = MagicMock()
    # Mock ping to raise Exception.
    # Since it is called via to_thread, the exception will be raised when awaited.
    mock_redis.ping.side_effect = Exception("Connection refused")

    with patch('app.health.redis_client', mock_redis):
        status = await app.health.check_redis_health()

        assert status.status == "degraded"
        assert "Redis connection error" in status.message
        assert status.details["ping"] == "failed"
        assert status.details["error"] == "Connection refused"

@pytest.mark.asyncio
async def test_check_redis_health_info_timeout():
    """Test handling of info timeout."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    # Use a closure to maintain state
    call_count = 0
    def side_effect(coro, timeout=None):
        nonlocal call_count
        call_count += 1
        coro.close()

        if call_count == 1:
            return True
        else:
            raise asyncio.TimeoutError("Timeout")

    with patch('app.health.redis_client', mock_redis):
        # First call (ping) returns success, second (info) raises TimeoutError
        with patch('asyncio.wait_for', side_effect=side_effect):
            status = await app.health.check_redis_health()

            assert status.status == "degraded"
            assert status.message == "Redis is connected but info unavailable"
            assert status.details["ping"] == "success"
            assert status.details["info"] == "timeout"

@pytest.mark.asyncio
async def test_check_redis_health_info_error():
    """Test handling of info error."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.info.side_effect = Exception("Info failed")

    with patch('app.health.redis_client', mock_redis):
        status = await app.health.check_redis_health()

        assert status.status == "degraded"
        assert status.message == "Redis is connected but info unavailable"
        assert status.details["ping"] == "success"
        assert status.details["info"] == "failed"
        assert status.details["error"] == "Info failed"

@pytest.mark.asyncio
async def test_check_redis_health_unexpected_error():
    """Test handling of unexpected error."""
    # Simulate an error that is not caught by inner try-except blocks.
    # We trigger this by making the HealthStatus constructor raise an exception
    # when called at the end of the success path.

    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    # The first call to HealthStatus (in success path) raises Exception
    # The second call (in exception handler) returns a mock object

    mock_error_status = MagicMock()
    mock_error_status.status = "unhealthy"
    mock_error_status.message = "Redis health check failed: Constructor failed"
    # Note: details is a dict, but on the mock it's an attribute.
    # The code does: details={"error": str(e)}
    # We can't easily check details contents on the mock unless we configure it or use side_effect to return a real object.
    # Let's assume the returned object is just a mock with attributes.
    mock_error_status.details = {"error": "Constructor failed"}

    with patch('app.health.redis_client', mock_redis):
        # We also need to patch time.time because exception handler uses start_time
        # which is initialized at start.
        # But wait, start_time is local variable. It is initialized fine.

        with patch('app.health.HealthStatus', side_effect=[Exception("Constructor failed"), mock_error_status]):
             status = await app.health.check_redis_health()

             assert status.status == "unhealthy"
             assert "Constructor failed" in status.message
             assert status.details["error"] == "Constructor failed"
