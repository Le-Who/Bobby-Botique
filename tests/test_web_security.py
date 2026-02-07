
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

def test_query_param_auth_accepted(client):
    """Test that query parameter authentication is accepted (for backward compatibility)"""
    endpoints = ['/', '/status', '/keys']
    for endpoint in endpoints:
        # Pass token in query string, NOT headers
        response = client.get(f"{endpoint}?token=test_token")
        # 200 or 500 means auth passed
        assert response.status_code != 401

def test_security_headers(client):
    """Test that security headers are present in responses"""
    # Test on a public endpoint
    response = client.get('/health')
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'

    csp = response.headers['Content-Security-Policy']
    assert "default-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "font-src 'self' https://fonts.gstatic.com" in csp

    assert response.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'

    # Test on a protected endpoint (even if 401, headers should be there if after_request runs)
    # Note: Flask after_request runs even on errors usually, unless handled by error handler that returns new response without them.
    # But let's check authorized response too.
    response_auth = client.get('/status', headers={'X-Auth-Token': 'test_token'})
    assert response_auth.headers['X-Content-Type-Options'] == 'nosniff'

def test_public_health_endpoint(client):
    """Test that /health is public"""
    response = client.get('/health')
    assert response.status_code != 401
