import pytest
import sys
from unittest.mock import MagicMock, patch

# Mock dependencies globally before any import
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
_original_modules = {k: sys.modules[k] for k in _mock_keys if k in sys.modules}

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


def teardown_module(module):
    for k in _mock_keys:
        if k in sys.modules:
            del sys.modules[k]
    sys.modules.update(_original_modules)


# Mock app.database
mock_db = MagicMock()
mock_db.db_pool = None
sys.modules["app.database"] = mock_db


@pytest.fixture
def client():
    # Mock settings before importing web
    with patch("app.config.settings") as mock_settings:
        mock_settings.TELEGRAM_BOT_TOKEN = "test_token"
        mock_settings.ADMIN_ID = 123
        mock_settings.ADMIN_SECRET = "test_token"

        # We need to reload or import app.web here to ensure patches are picked up
        if "app.web" in sys.modules:
            del sys.modules["app.web"]

        from app.web import flask_app

        flask_app.config["TESTING"] = True

        # Mock psutil for endpoints that use it
        with (
            patch("psutil.cpu_percent", return_value=10),
            patch("psutil.virtual_memory") as mock_vm,
            patch("psutil.disk_usage") as mock_du,
        ):
            mock_vm.return_value.percent = 20
            mock_du.return_value.percent = 30

            with flask_app.test_client() as client:
                yield client


def test_security_headers_present(client):
    """Test that security headers are present in responses"""
    # Test public endpoint
    response = client.get("/health")
    assert response.status_code == 200

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    csp = headers.get("Content-Security-Policy")
    assert csp is not None
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "font-src 'self' https://fonts.gstatic.com" in csp


def test_security_headers_on_error(client):
    """Test that security headers are present even on error responses"""
    # Force an error by mocking something to raise exception, or just use 404
    response = client.get("/non-existent-endpoint")
    assert response.status_code == 404

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"


def test_security_headers_on_auth_failure(client):
    """Test that security headers are present on 401"""
    response = client.get("/")
    assert response.status_code == 401

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    """Test that security headers are present even on error pages"""
    # Force an error by accessing a non-existent route or causing an exception
    # Since we are testing headers, a 404 is a good candidate
    response = client.get("/non-existent-route")

    assert response.status_code == 404
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
