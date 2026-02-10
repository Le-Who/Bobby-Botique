
import pytest
import sys
import os
import importlib
from unittest.mock import MagicMock, AsyncMock, patch

# Mock dependencies globally before any import
sys.modules['asyncpg'] = MagicMock()
sys.modules['asyncpg.pool'] = MagicMock()
sys.modules['google.genai'] = MagicMock()
sys.modules['google.genai.errors'] = MagicMock()
sys.modules['redis'] = MagicMock()
sys.modules['redis.exceptions'] = MagicMock()
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()
sys.modules['telegram.error'] = MagicMock()
sys.modules['hypercorn.config'] = MagicMock()
sys.modules['hypercorn.asyncio'] = MagicMock()

# Mock psutil with return values
mock_psutil = MagicMock()
mock_psutil.cpu_percent.return_value = 10.0
mock_psutil.virtual_memory.return_value.percent = 20.0
mock_psutil.disk_usage.return_value.percent = 30.0
sys.modules['psutil'] = mock_psutil

# Mock app.database
mock_db = MagicMock()
mock_db.db_pool = None
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
    # Reload app.web to ensure it picks up the patched settings (or we patch app.web.settings)
    # Since we use patch('app.config.settings'), if app.web imports it, we need to ensure app.web uses the patched version.

    # Force reload of app.web if it's already imported
    if 'app.web' in sys.modules:
        importlib.reload(sys.modules['app.web'])
    else:
        import app.web

    # Explicitly patch settings in app.web to be sure
    with patch('app.web.settings', mock_settings):
        from app.web import flask_app
        flask_app.config['TESTING'] = True
        yield flask_app.test_client()

def test_unauthorized_access(client):
    """Test that unauthorized access returns 401"""
    endpoints = ['/', '/status', '/keys', '/keys/gemini-2.5-flash']
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 401
        assert b"Unauthorized" in response.data

def test_authorized_access(client):
    """Test that authorized access with correct token works"""
    endpoints = ['/', '/status', '/keys', '/keys/gemini-2.5-flash']
    headers = {'X-Auth-Token': 'test_token'}

    for endpoint in endpoints:
        response = client.get(endpoint, headers=headers)
        # 200 or 500 (if internal logic fails due to missing mocks like psutil) means auth passed
        assert response.status_code != 401

def test_invalid_token(client):
    """Test that invalid token returns 401"""
    endpoints = ['/', '/status', '/keys', '/keys/gemini-2.5-flash']
    headers = {'X-Auth-Token': 'wrong_token'}
    for endpoint in endpoints:
        response = client.get(endpoint, headers=headers)
        assert response.status_code == 401

def test_public_health_endpoint(client):
    """Test that /health is public"""
    response = client.get('/health')
    assert response.status_code != 401

def test_health_endpoint_no_info_leak(client):
    """Test that /health does NOT leak container_id and process_id"""
    response = client.get('/health')
    # Can be 200 (healthy/degraded) or 503 (unhealthy) depending on mocks
    assert response.status_code in [200, 503]
    data = response.json

    assert 'container_id' not in data, "container_id should not be exposed"
    assert 'process_id' not in data, "process_id should not be exposed"

def test_security_headers_present(client):
    """Test that security headers are present on all responses"""
    response = client.get('/health')
    headers = response.headers

    assert headers.get('X-Content-Type-Options') == 'nosniff'
    assert headers.get('X-Frame-Options') == 'DENY'
    # Allow inline styles for progress bars and Google Fonts
    assert headers.get('Content-Security-Policy') == "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com"
