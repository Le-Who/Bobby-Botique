import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Mock external dependencies
class MockBaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

sys.modules["pytz"] = MagicMock()
pydantic = MagicMock()
pydantic.BaseModel = MockBaseModel
sys.modules["pydantic"] = pydantic
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["asyncpg"] = MagicMock()
sys.modules["google"] = MagicMock()
sys.modules["google.genai"] = MagicMock()
sys.modules["httpx"] = MagicMock()
sys.modules["tavily"] = MagicMock()
sys.modules["cachetools"] = MagicMock()
sys.modules["psutil"] = MagicMock()
sys.modules["orjson"] = MagicMock()

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

# Mock local dependencies
sys.modules["app.utils.formatting"] = MagicMock()
mock_formatter = MagicMock()
mock_formatter.format_text.return_value = ("formatted", "Markdown")
sys.modules["app.utils.formatting"].TelegramFormatter = mock_formatter

from app.alerts import AlertManager, run_alert_checks, send_alerts_to_admin

@pytest.fixture
def alert_manager():
    with patch("app.alerts.settings") as mock_settings:
        mock_settings.ALERT_COOLDOWN_SECONDS = 3600
        manager = AlertManager()
        manager.alert_cooldown = 3600
        return manager

def test_should_send_alert(alert_manager):
    async def run():
        alert_key = "test_alert"
        assert await alert_manager._should_send_alert(alert_key) is True
        await alert_manager._mark_alert_sent(alert_key)
        assert await alert_manager._should_send_alert(alert_key) is False

        with patch("app.alerts.datetime") as mock_datetime:
            now = datetime.now()
            mock_datetime.now.return_value = now + timedelta(seconds=3601)
            assert await alert_manager._should_send_alert(alert_key) is True
    asyncio.run(run())

def test_mark_alert_sent(alert_manager):
    async def run():
        alert_key = "test_alert"
        await alert_manager._mark_alert_sent(alert_key)
        assert alert_key in alert_manager.sent_alerts
        assert alert_key in alert_manager.last_alert_time
        assert isinstance(alert_manager.last_alert_time[alert_key], datetime)
    asyncio.run(run())

def test_check_gemini_limits(alert_manager):
    async def run():
        with patch("app.alerts.db.db_query", new_callable=AsyncMock) as mock_query, \
             patch("app.alerts.settings") as mock_settings, \
             patch("app.alerts.time_utils.get_pacific_date") as mock_date:

            mock_date.return_value = "2023-10-26"
            mock_settings.DAILY_LIMITS = {"model1": 100}
            mock_settings.LIMIT_THRESHOLD_PERCENT = 80

            mock_query.side_effect = [
                [{"key_hash": "hash1", "api_key": "key1_secret"}],
                [{"request_count": 85}]
            ]

            alerts = await alert_manager.check_gemini_limits()
            assert len(alerts) == 1
            assert "Gemini API Limit" in alerts[0]
    asyncio.run(run())

def test_check_tavily_limits(alert_manager):
    async def run():
        with patch("app.alerts.db.db_query", new_callable=AsyncMock) as mock_query, \
             patch("app.alerts.settings") as mock_settings, \
             patch("app.alerts.time_utils.get_current_month_str") as mock_month:

            mock_month.return_value = "2023-10"
            mock_settings.TAVILY_MONTHLY_CREDIT_LIMIT = 1000
            mock_settings.TAVILY_LIMIT_THRESHOLD_PERCENT = 90

            mock_query.side_effect = [
                [{"key_hash": "hash1", "api_key": "tavily_key1"}],
                [{"credit_usage": 950}]
            ]

            alerts = await alert_manager.check_tavily_limits()
            assert len(alerts) == 1
            assert "Tavily API Limit" in alerts[0]
    asyncio.run(run())

def test_check_no_available_keys(alert_manager):
    async def run():
        with patch("app.alerts.db.get_available_gemini_key", new_callable=AsyncMock) as mock_gemini, \
             patch("app.alerts.db.get_available_tavily_key", new_callable=AsyncMock) as mock_tavily, \
             patch("app.alerts.settings") as mock_settings:

            mock_settings.AVAILABLE_MODELS = ["model1"]
            mock_gemini.return_value = None
            mock_tavily.return_value = {"api_key": "tavily_key"}

            alerts = await alert_manager.check_no_available_keys()
            assert len(alerts) == 1
            assert "No Available Keys" in alerts[0]
    asyncio.run(run())

def test_clear_old_alerts(alert_manager):
    async def run():
        alert_key = "old_alert"
        alert_manager.sent_alerts.add(alert_key)
        alert_manager.last_alert_time[alert_key] = datetime.now() - timedelta(hours=25)
        await alert_manager.clear_old_alerts()
        assert alert_key not in alert_manager.sent_alerts
    asyncio.run(run())

def test_run_alert_checks():
    async def run():
        from app.alerts import alert_manager as global_manager
        with patch.object(global_manager, "check_gemini_limits", new_callable=AsyncMock) as mock_gemini, \
             patch.object(global_manager, "check_tavily_limits", new_callable=AsyncMock) as mock_tavily, \
             patch.object(global_manager, "check_no_available_keys", new_callable=AsyncMock) as mock_no_keys:

            mock_gemini.return_value = ["alert1"]
            mock_tavily.return_value = ["alert2"]
            mock_no_keys.return_value = ["alert3"]

            alerts = await run_alert_checks()
            assert len(alerts) == 3
    asyncio.run(run())

def test_send_alerts_to_admin():
    async def run():
        mock_context = MagicMock()
        mock_context.bot.send_message = AsyncMock()
        with patch("app.alerts.run_alert_checks", new_callable=AsyncMock) as mock_checks, \
             patch("app.alerts.settings") as mock_settings:
            mock_checks.return_value = ["alert1"]
            mock_settings.ADMIN_ID = 12345
            await send_alerts_to_admin(mock_context)
            assert mock_context.bot.send_message.called
    asyncio.run(run())
