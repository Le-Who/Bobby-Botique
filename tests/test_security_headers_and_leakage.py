import pytest
import sys
import os
import importlib
from unittest.mock import MagicMock, AsyncMock, patch
from flask import Flask

# Mock dependencies globally before any import
sys.modules['asyncpg'] = MagicMock()
sys.modules['asyncpg.pool'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['redis'] = MagicMock()
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()
sys.modules['hypercorn.config'] = MagicMock()
sys.modules['hypercorn.asyncio'] = MagicMock()

# Mock app.database
mock_db = MagicMock()
mock_db.is_database_connected = MagicMock(return_value=True)
mock_db.get_gemini_key_usage_stats = AsyncMock(return_value=[])
mock_db.get_active_key_info = AsyncMock(return_value={})
sys.modules['app.database'] = mock_db

# Mock app.config settings
@pytest.fixture
def mock_settings():
    with patch('app.config.settings') as mock:
        mock.TELEGRAM_BOT_TOKEN = "test_token"
        mock.ADMIN_ID = 123
        mock.DAILY_LIMITS = {}
        mock.PORT = 5000
        yield mock

@pytest.fixture
def client(mock_settings):
    # We must patch sys.modules to ensure app.web uses our mocks
    # even if it was imported before
    with patch.dict(sys.modules, {'app.database': mock_db}):
        if 'app.web' in sys.modules:
            del sys.modules['app.web']

        import app.web
        # Patch settings inside app.web
        with patch('app.web.settings', mock_settings):
            app.web.flask_app.config['TESTING'] = True
            yield app.web.flask_app.test_client()

def test_security_headers(client):
    """Test that security headers are present in responses"""
    response = client.get('/health')
    assert response.status_code in [200, 503]

    headers = response.headers
    # Expect these to fail initially
    assert headers.get('X-Content-Type-Options') == 'nosniff'
    assert headers.get('X-Frame-Options') == 'DENY'
    assert headers.get('Referrer-Policy') == 'strict-origin-when-cross-origin'
    assert "default-src 'self'" in headers.get('Content-Security-Policy', '')
    assert "script-src 'none'" in headers.get('Content-Security-Policy', '')
    assert "style-src 'self' 'unsafe-inline'" in headers.get('Content-Security-Policy', '')

def test_health_endpoint_leakage(client):
    """Test that /health does not leak sensitive info"""
    response = client.get('/health')
    data = response.get_json()

    # Expect these to fail initially (as they ARE currently present)
    assert 'container_id' not in data
    assert 'process_id' not in data
    assert 'services' in data

def test_error_handling_leakage(client):
    """Test that exceptions do not leak stack traces in /health"""
    # Force an exception
    with patch('app.database.is_database_connected', side_effect=Exception("Sensitive DB Error Details")):
        # Because we reload app.web in the fixture, app.web.database is likely bound to the mock_db instance
        # So patching app.database.is_database_connected *should* work if app.web calls app.database.is_database_connected
        # But app.web does `from app import database` so it holds a reference to the module.
        # Patching `app.database.is_database_connected` modifies the function on the module, so it should propagate.

        response = client.get('/health')
        assert response.status_code == 500
        data = response.get_json()
        assert data['status'] == 'unhealthy'
        # Currently it returns str(e), so "Sensitive DB Error Details"
        # We want "Internal Health Check Error" and NOT "Sensitive DB Error Details"
        assert data['error'] == 'Internal Health Check Error'
        assert "Sensitive DB Error Details" not in data['error']

def test_dashboard_error_leakage(client):
    """Test that dashboard exceptions do not leak stack traces"""
    headers = {'X-Auth-Token': 'test_token'}

    with patch('app.database.is_database_connected', side_effect=Exception("Sensitive Dashboard Error")):
        response = client.get('/', headers=headers)
        assert response.status_code == 500
        # Currently returns "Dashboard Error: Sensitive Dashboard Error"
        assert b"Sensitive Dashboard Error" not in response.data
        assert b"Internal Server Error" in response.data
