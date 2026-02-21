import pytest
import sys
import importlib
from unittest.mock import MagicMock, patch

# Mock dependencies globally before any import (copied from test_web_security.py)
sys.modules["asyncpg"] = MagicMock()
sys.modules["asyncpg.pool"] = MagicMock()
sys.modules["google.genai"] = MagicMock()
sys.modules["google.genai.errors"] = MagicMock()
sys.modules["redis"] = MagicMock()
sys.modules["redis.exceptions"] = MagicMock()
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
sys.modules["telegram.error"] = MagicMock()
sys.modules["hypercorn.config"] = MagicMock()
sys.modules["hypercorn.asyncio"] = MagicMock()
sys.modules["pytz"] = MagicMock()

# Mock app.database
mock_db = MagicMock()
mock_db.db_pool = None
mock_db.get_gemini_key_usage_stats = MagicMock(return_value=[])
mock_db.get_active_key_info = MagicMock(return_value={})
sys.modules["app.database"] = mock_db

# Ensure app.config is imported


@pytest.fixture
def client():
    # Mock settings
    with patch("app.config.settings") as mock_settings:
        mock_settings.TELEGRAM_BOT_TOKEN = "test_secret_token"
        mock_settings.ADMIN_SECRET = "test_secret_token"  # Explicitly set for clarity

        # Ensure app.web uses these settings
        if "app.web" in sys.modules:
            importlib.reload(sys.modules["app.web"])
        else:
            pass

        with patch("app.web.settings", mock_settings):
            from app.web import flask_app

            flask_app.config["TESTING"] = True
            yield flask_app.test_client()


def test_query_param_auth_rejected(client):
    """
    Test that authentication via query parameter is REJECTED (vulnerability fixed).
    """
    # Verify accessing protected endpoint with query param
    response = client.get("/status?token=test_secret_token")

    # DESIRED BEHAVIOR: 401 Unauthorized
    assert response.status_code == 401
    assert b"Unauthorized" in response.data
    assert b"X-Auth-Token" in response.data


def test_header_auth_works(client):
    """Verify header auth still works"""
    response = client.get("/status", headers={"X-Auth-Token": "test_secret_token"})
    assert response.status_code != 401
