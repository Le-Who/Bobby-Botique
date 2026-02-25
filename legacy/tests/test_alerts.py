# ruff: noqa: E402
"""
Tests for app.alerts module.
Standard patching allows tests to run fast without hitting real databases,
while preserving sys.modules state across the test suite.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# Ensure app.config.settings has the fields we expect before importing AlertsManager
# (this is safe standard patching because we don't manipulate sys.modules globally)
import app.config

original_settings = app.config.settings
mock_settings = MagicMock()
mock_settings.ALERT_COOLDOWN_SECONDS = 3600
app.config.settings = mock_settings

from app.alerts import AlertManager, run_alert_checks, send_alerts_to_admin
import app.alerts as _alerts_module

# Restore the original settings object back to the module to not affect other tests
app.config.settings = original_settings


@pytest.fixture
def alert_manager():
    """Create a fresh AlertManager with mocked settings for each test."""
    with patch.object(_alerts_module, "settings") as mock_s:
        mock_s.ALERT_COOLDOWN_SECONDS = 3600
        manager = AlertManager()
        manager.alert_cooldown = 3600
        manager._lock = asyncio.Lock()
        return manager


# ── _should_send_alert / _mark_alert_sent ────────────────────────────────


@pytest.mark.asyncio
async def test_should_send_alert(alert_manager):
    alert_key = "test_alert"
    assert await alert_manager._should_send_alert(alert_key) is True

    await alert_manager._mark_alert_sent(alert_key)
    assert await alert_manager._should_send_alert(alert_key) is False

    # Simulate cooldown expiry
    alert_manager.last_alert_time[alert_key] = datetime.now() - timedelta(seconds=3601)
    assert await alert_manager._should_send_alert(alert_key) is True


@pytest.mark.asyncio
async def test_mark_alert_sent(alert_manager):
    alert_key = "test_alert"
    await alert_manager._mark_alert_sent(alert_key)
    assert alert_key in alert_manager.sent_alerts
    assert alert_key in alert_manager.last_alert_time
    assert isinstance(alert_manager.last_alert_time[alert_key], datetime)


# ── check_gemini_limits ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_gemini_limits(alert_manager):
    with (
        patch.object(_alerts_module, "db") as mock_db,
        patch.object(_alerts_module, "settings") as mock_settings,
        patch.object(_alerts_module, "time_utils") as mock_time,
    ):
        mock_time.get_pacific_date.return_value = "2023-10-26"
        mock_settings.DAILY_LIMITS = {"model1": 100}
        mock_settings.LIMIT_THRESHOLD_PERCENT = 80
        mock_settings.ALERT_COOLDOWN_SECONDS = 3600

        mock_db.db_query = AsyncMock(
            side_effect=[
                [{"key_hash": "hash1", "api_key": "key1_secret"}],
                [{"request_count": 85}],
            ]
        )

        alerts = await alert_manager.check_gemini_limits()
        assert len(alerts) == 1
        assert "Gemini API Limit" in alerts[0]


# ── check_tavily_limits ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_tavily_limits(alert_manager):
    with (
        patch.object(_alerts_module, "db") as mock_db,
        patch.object(_alerts_module, "settings") as mock_settings,
        patch.object(_alerts_module, "time_utils") as mock_time,
    ):
        mock_time.get_current_month_str.return_value = "2023-10"
        mock_settings.TAVILY_MONTHLY_CREDIT_LIMIT = 1000
        mock_settings.TAVILY_LIMIT_THRESHOLD_PERCENT = 90
        mock_settings.ALERT_COOLDOWN_SECONDS = 3600

        mock_db.db_query = AsyncMock(
            side_effect=[
                [{"key_hash": "hash1", "api_key": "tavily_key1"}],
                [{"credit_usage": 950}],
            ]
        )

        alerts = await alert_manager.check_tavily_limits()
        assert len(alerts) == 1
        assert "Tavily API Limit" in alerts[0]


# ── check_no_available_keys ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_no_available_keys(alert_manager):
    with (
        patch.object(_alerts_module, "db") as mock_db,
        patch.object(_alerts_module, "settings") as mock_settings,
    ):
        mock_settings.AVAILABLE_MODELS = ["model1"]
        mock_settings.ALERT_COOLDOWN_SECONDS = 3600
        mock_db.get_available_gemini_key = AsyncMock(return_value=None)
        mock_db.get_available_tavily_key = AsyncMock(
            return_value={"api_key": "tavily_key"}
        )

        alerts = await alert_manager.check_no_available_keys()
        assert len(alerts) == 1
        assert "No Available Keys" in alerts[0]


# ── clear_old_alerts ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_old_alerts(alert_manager):
    alert_key = "old_alert"
    alert_manager.sent_alerts.add(alert_key)
    alert_manager.last_alert_time[alert_key] = datetime.now() - timedelta(hours=25)
    await alert_manager.clear_old_alerts()
    assert alert_key not in alert_manager.sent_alerts


# ── run_alert_checks ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_alert_checks():
    global_manager = _alerts_module.alert_manager

    with (
        patch.object(
            global_manager, "check_gemini_limits", new_callable=AsyncMock
        ) as mock_gemini,
        patch.object(
            global_manager, "check_tavily_limits", new_callable=AsyncMock
        ) as mock_tavily,
        patch.object(
            global_manager, "check_no_available_keys", new_callable=AsyncMock
        ) as mock_no_keys,
    ):
        mock_gemini.return_value = ["alert1"]
        mock_tavily.return_value = ["alert2"]
        mock_no_keys.return_value = ["alert3"]

        alerts = await run_alert_checks()
        assert len(alerts) == 3


# ── send_alerts_to_admin ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_alerts_to_admin():
    mock_context = MagicMock()
    mock_context.bot.send_message = AsyncMock()

    with (
        patch.object(
            _alerts_module, "run_alert_checks", new_callable=AsyncMock
        ) as mock_checks,
        patch.object(_alerts_module, "settings") as mock_settings,
        patch.object(_alerts_module, "TelegramFormatter") as mock_fmt,
    ):
        mock_checks.return_value = ["alert1"]
        mock_settings.ADMIN_ID = 12345
        mock_fmt.format_text.return_value = ("formatted", "Markdown")

        await send_alerts_to_admin(mock_context)
        assert mock_context.bot.send_message.called
