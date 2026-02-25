import pytest
import re
import sys
import importlib
from unittest.mock import MagicMock, AsyncMock, patch

# Mock dependencies to load app.web
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
    "psutil",
    "app.database",
]
_original_modules = {}


def setup_module(module):
    global _original_modules
    for k in _mock_keys:
        if k in sys.modules:
            _original_modules[k] = sys.modules[k]
        sys.modules[k] = MagicMock()

    # Specialized mocks
    mock_psutil = MagicMock()
    mock_psutil.cpu_percent.return_value = 10.0
    mock_psutil.virtual_memory.return_value.percent = 20.0
    sys.modules["psutil"] = mock_psutil

    mock_db = MagicMock()
    mock_db.is_database_connected.return_value = True
    sys.modules["app.database"] = mock_db


def teardown_module(module):
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)


@pytest.fixture
def client():
    # Force reload of app.web
    if "app.web" in sys.modules:
        importlib.reload(sys.modules["app.web"])

    with patch("app.config.settings") as mock_settings:
        mock_settings.ADMIN_SECRET = "test_token"

        # Explicitly patch settings in app.web
        with patch("app.web.settings", mock_settings):
            from app.web import flask_app
            flask_app.config["TESTING"] = True
            yield flask_app.test_client()


@pytest.mark.asyncio
async def test_dashboard_tabs_accessibility(client):
    """Verify that dashboard tabs have correct ARIA attributes."""
    headers = {"X-Auth-Token": "test_token"}
    response = await client.get("/", headers=headers)
    assert response.status_code == 200

    html = (await response.get_data()).decode()

    # Check for ARIA roles and attributes
    # 1. Navigation container should have role="tablist"
    assert 'role="tablist"' in html, "Missing role='tablist' on tab container"

    # 2. Tab buttons should have role="tab"
    assert 'role="tab"' in html, "Missing role='tab' on tab buttons"

    # 3. Tab buttons should have aria-selected
    assert 'aria-selected="true"' in html, "Missing aria-selected attribute"

    # 4. Tab buttons should have aria-controls
    assert 'aria-controls="panel-overview"' in html, "Missing aria-controls attribute"

    # 5. Tab panels should have role="tabpanel"
    assert 'role="tabpanel"' in html, "Missing role='tabpanel' on content panels"

    # 6. Tab panels should have aria-labelledby
    assert 'aria-labelledby="' in html, "Missing aria-labelledby attribute"
