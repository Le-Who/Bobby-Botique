import pytest
import sys
import importlib
from unittest.mock import MagicMock, patch

# Store mocked keys to clean up later
_mock_keys = [
    "asyncpg",
    "asyncpg.pool",
    "google.genai",
    "google.genai.errors",
    "redis",
    "redis.exceptions",
    "telegram",
    "telegram.ext",
    "telegram.error",
    "hypercorn.config",
    "hypercorn.asyncio",
    "pytz",
    "app.database",
]
_original_modules = {}


def setup_module(module):
    global _original_modules
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()

    mock_db = MagicMock()
    mock_db.db_pool = None
    mock_db.get_gemini_key_usage_stats = MagicMock(return_value=[])
    mock_db.get_active_key_info = MagicMock(return_value={})
    sys.modules["app.database"] = mock_db


def teardown_module(module):
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)


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
