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
sys.modules['psutil'] = MagicMock()

# Mock app.database
mock_db = MagicMock()
mock_db.db_pool = None
# Make is_database_connected return True so we get "connected" status
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
    # Force reload of app.web if it's already imported
    if 'app.web' in sys.modules:
        importlib.reload(sys.modules['app.web'])
    else:
        import app.web

    # Explicitly patch settings in app.web
    with patch('app.web.settings', mock_settings):
        from app.web import flask_app
        flask_app.config['TESTING'] = True
        yield flask_app.test_client()

def test_dashboard_ux_elements(client):
    """Test that dashboard includes UX and accessibility improvements"""
    headers = {'X-Auth-Token': 'test_token'}

    # Mock psutil calls inside the route
    with patch('psutil.cpu_percent', return_value=45.0), \
         patch('psutil.virtual_memory') as mock_vm, \
         patch('psutil.disk_usage') as mock_du:

        mock_vm.return_value.percent = 60.0
        mock_du.return_value.percent = 75.0

        response = client.get('/', headers=headers)

        assert response.status_code == 200
        html = response.data.decode('utf-8')

        # 1. Check for auto-refresh meta tag
        assert '<meta http-equiv="refresh" content="60">' in html, "Auto-refresh meta tag missing"

        # 2. Check for ARIA attributes on CPU progress bar
        # We look for the structure or specific attributes near the value
        assert 'role="progressbar"' in html, "Progress bar role missing"
        assert 'aria-label="CPU Usage"' in html, "CPU Usage ARIA label missing"
        assert 'aria-valuenow="45.0"' in html or 'aria-valuenow="45"' in html, "CPU aria-valuenow missing or incorrect"

        # 3. Check for ARIA attributes on Memory progress bar
        assert 'aria-label="Memory"' in html, "Memory ARIA label missing"
        assert 'aria-valuenow="60.0"' in html or 'aria-valuenow="60"' in html, "Memory aria-valuenow missing"

        # 4. Check for ARIA attributes on Disk progress bar
        assert 'aria-label="Disk Storage"' in html, "Disk Storage ARIA label missing"
        assert 'aria-valuenow="75.0"' in html or 'aria-valuenow="75"' in html, "Disk aria-valuenow missing"

        # 5. Check for aria-hidden on status indicators
        # We need to find the status indicator spans
        # Using a simple string check might be enough if unique
        assert 'class="status-indicator status-running" aria-hidden="true"' in html or \
               'class="status-indicator status-running" aria-hidden=\'true\'' in html or \
               ('class="status-indicator status-running"' in html and 'aria-hidden="true"' in html), \
               "Status indicator should have aria-hidden='true'"
