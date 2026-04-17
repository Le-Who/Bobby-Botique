import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# Isolate in dedicated xdist worker — setup_module mutates sys.modules.
pytestmark = pytest.mark.xdist_group("sys_modules_isolation")

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
    "app.database",
]
_original_modules = {}


def setup_module(module):
    global _original_modules
    _original_modules["__app_keys_before__"] = {k for k in sys.modules if k.startswith("app.")}
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()

    mock_db = MagicMock()
    mock_db.get_gemini_key_usage_stats = MagicMock(return_value=[])
    mock_db.get_active_key_info = MagicMock(return_value={})
    sys.modules["app.database"] = mock_db


def teardown_module(module):
    app_keys_before = _original_modules.pop("__app_keys_before__", set())
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)
    for k in list(sys.modules):
        if k.startswith("app.") and k not in app_keys_before:
            del sys.modules[k]


@pytest.fixture
def client():
    # Mock settings
    with patch("app.config.settings") as mock_settings:
        mock_settings.TELEGRAM_BOT_TOKEN = "test_secret_token"
        mock_settings.ADMIN_SECRET = "test_secret_token"  # Explicitly set for clarity

        # Ensure app.web uses these settings
        if "app.web" in sys.modules:
            importlib.reload(sys.modules["app.web"])

        with patch("app.web.settings", mock_settings):
            from app.web import quart_app

            quart_app.config["TESTING"] = True
            yield quart_app.test_client()


@pytest.mark.asyncio
async def test_query_param_auth_rejected(client):
    """
    Test that authentication via query parameter is REJECTED (vulnerability fixed).
    """
    # Verify accessing protected API endpoint with query param (not header)
    response = await client.get("/api/overview?token=test_secret_token")

    # DESIRED BEHAVIOR: 401 Unauthorized (query params ignored for auth)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_header_auth_works(client):
    """Verify header auth still works"""
    response = await client.get("/api/overview", headers={"X-Auth-Token": "test_secret_token"})
    assert response.status_code != 401
