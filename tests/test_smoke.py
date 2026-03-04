"""End-to-end smoke tests — verify bot subsystems work together.

Tests the full stack without real Telegram/DB connections:
- Quart health & metrics endpoints
- Bot handler registration
- Prometheus metrics generation
- Admin alerts startup flow
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# 1. QUART HEALTH ENDPOINT
# ============================================================================


class TestHealthEndpoint:
    """Test the /health endpoint returns correct structure."""

    @pytest.fixture
    def app(self):
        from app.web import quart_app

        quart_app.config["TESTING"] = True
        return quart_app

    @pytest.mark.asyncio
    async def test_health_returns_200_structure(self, app):
        """Health endpoint should return valid JSON with service statuses."""
        async with app.test_client() as client:
            resp = await client.get("/health")
            data = await resp.get_json()

            assert resp.status_code in (200, 503)  # 503 if DB not connected
            assert "status" in data
            assert data["status"] in ("healthy", "unhealthy")
            assert "services" in data
            assert "bot" in data["services"]
            assert "database" in data["services"]
            assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_health_includes_redis_status(self, app):
        """Health endpoint should report Redis status."""
        async with app.test_client() as client:
            resp = await client.get("/health")
            data = await resp.get_json()

            assert "redis" in data["services"]
            assert data["services"]["redis"] in ("connected", "disconnected", "not_configured", "unknown")


# ============================================================================
# 2. PROMETHEUS METRICS ENDPOINT
# ============================================================================


class TestMetricsEndpoint:
    """Test the /metrics Prometheus endpoint."""

    @pytest.fixture
    def app(self):
        from app.web import quart_app

        quart_app.config["TESTING"] = True
        return quart_app

    @pytest.mark.asyncio
    async def test_metrics_returns_prometheus_format(self, app):
        """Metrics endpoint should return Prometheus text format."""
        async with app.test_client() as client:
            resp = await client.get("/metrics")

            assert resp.status_code == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/plain" in content_type

            text = await resp.get_data(as_text=True)
            assert "gembot_uptime_seconds" in text
            assert "# HELP" in text
            assert "# TYPE" in text

    @pytest.mark.asyncio
    async def test_metrics_includes_memory(self, app):
        """Metrics should include process memory gauge."""
        async with app.test_client() as client:
            resp = await client.get("/metrics")
            text = await resp.get_data(as_text=True)
            assert "gembot_process_memory_bytes" in text


# ============================================================================
# 3. PROMETHEUS GENERATOR UNIT TEST
# ============================================================================


class TestPrometheusGenerator:
    """Test prometheus.py generates valid text exposition."""

    def test_generate_metrics_text_format(self):
        """generate_metrics_text() should produce valid Prometheus format."""
        from app.prometheus import generate_metrics_text

        text = generate_metrics_text()

        assert isinstance(text, str)
        assert text.endswith("\n")
        # Check required metrics exist
        assert "gembot_uptime_seconds" in text
        assert "gembot_process_memory_bytes" in text
        assert "gembot_active_users" in text

        # Check format: every non-empty, non-comment line has metric_name value
        for line in text.strip().split("\n"):
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            assert len(parts) >= 2, f"Invalid metric line: {line}"


# ============================================================================
# 4. BOT HANDLER REGISTRATION
# ============================================================================


class TestBotHandlerRegistration:
    """Verify all handler modules register without errors."""

    def test_commands_register(self):
        """commands.register() should not raise on a mock application."""
        from app.handlers import commands

        mock_app = MagicMock()
        mock_app.add_handler = MagicMock()

        # Should not raise
        commands.register(mock_app)
        assert mock_app.add_handler.call_count > 0

    def test_callbacks_register(self):
        """callbacks.register() should not raise on a mock application."""
        from app.handlers import callbacks

        mock_app = MagicMock()
        mock_app.add_handler = MagicMock()

        callbacks.register(mock_app)
        assert mock_app.add_handler.call_count > 0

    def test_messages_register(self):
        """messages.register() should not raise on a mock application."""
        from app.handlers import messages

        mock_app = MagicMock()
        mock_app.add_handler = MagicMock()

        messages.register(mock_app)
        assert mock_app.add_handler.call_count > 0


# ============================================================================
# 5. ADMIN ALERTS STARTUP FLOW
# ============================================================================


class TestAdminAlertsStartupFlow:
    """Test the startup/shutdown alert flow end-to-end."""

    @pytest.mark.asyncio
    async def test_startup_alert_sends_health_report(self):
        """alert_admin_startup() should send a message with system status."""
        from app.admin_alerts import _alert_timestamps, alert_admin_startup

        _alert_timestamps.clear()

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            await alert_admin_startup(mock_app)

        mock_app.bot.send_message.assert_called_once()
        call_kwargs = mock_app.bot.send_message.call_args[1]
        text = call_kwargs["text"]
        assert "🟢" in text or "started" in text.lower() or "запущен" in text.lower()

        _alert_timestamps.clear()

    @pytest.mark.asyncio
    async def test_shutdown_alert_sends_notification(self):
        """alert_admin_shutdown() should send shutdown notification."""
        from app.admin_alerts import _alert_timestamps, alert_admin_shutdown

        _alert_timestamps.clear()

        mock_app = MagicMock()
        mock_app.bot = AsyncMock()

        with patch("app.config.settings", MagicMock(ADMIN_ID=12345)):
            await alert_admin_shutdown(mock_app, reason="test")

        mock_app.bot.send_message.assert_called_once()
        call_kwargs = mock_app.bot.send_message.call_args[1]
        text = call_kwargs["text"]
        assert "🔴" in text or "shutdown" in text.lower() or "остановлен" in text.lower()

        _alert_timestamps.clear()


# ============================================================================
# 6. FULL PIPELINE SMOKE: error tag → detect → prometheus counter
# ============================================================================


class TestFullPipelineSmoke:
    """Smoke test: error flows through the entire pipeline."""

    @pytest.mark.asyncio
    async def test_error_flows_through_pipeline(self):
        """Tag an error, detect it, record metric — verify end-to-end."""
        from app.errors import ErrorCode, is_error_message, tag_error
        from app.metrics import metrics_collector

        # 1. Tag an error
        error_text = tag_error(ErrorCode.RATE_LIMIT, "Test rate limit")

        # 2. Detect it
        assert is_error_message(error_text) is True

        # 3. Record directly via _process_event (queue consumer is a background task)
        metrics_collector._process_event(
            {
                "type": "error",
                "error_type": "rate_limit",
                "error_message": "Test rate limit",
                "request_id": "smoke-test",
                "timestamp": __import__("time").time(),
            }
        )

        # 4. Verify error was logged
        assert len(metrics_collector.error_log) > 0
        last_error = list(metrics_collector.error_log)[-1]
        assert "rate_limit" in str(last_error)
