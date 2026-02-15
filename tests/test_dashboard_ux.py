import unittest
import sys
import os
from unittest.mock import MagicMock, patch

# Ensure app can be imported
sys.path.append(os.getcwd())

class TestDashboardUX(unittest.TestCase):
    def setUp(self):
        # Create mocks for dependencies
        self.mock_database = MagicMock()
        self.mock_config = MagicMock()
        self.mock_config.settings = MagicMock()
        self.mock_config.settings.TELEGRAM_BOT_TOKEN = "test_token"

        self.mock_request_context = MagicMock()
        self.mock_tracing = MagicMock()

        # Patch sys.modules
        self.modules_patcher = patch.dict('sys.modules', {
            'app.database': self.mock_database,
            'app.config': self.mock_config,
            'app.request_context': self.mock_request_context,
            'app.tracing': self.mock_tracing,
            'app.cache': MagicMock(),
            'asyncpg': MagicMock(),
            'google.genai': MagicMock(),
            'redis': MagicMock(),
            'telegram': MagicMock(),
        })
        self.modules_patcher.start()

        # Remove app.web from sys.modules to force re-import with mocks
        if 'app.web' in sys.modules:
            del sys.modules['app.web']

        # Import app.web using the mocked dependencies
        import app.web
        self.app = app.web.flask_app
        self.client = self.app.test_client()

    def tearDown(self):
        # Stop patching sys.modules
        self.modules_patcher.stop()

        # Remove the mocked app.web from sys.modules
        # This ensures subsequent tests import the real app.web (or their own mocked version)
        if 'app.web' in sys.modules:
            del sys.modules['app.web']

    @patch('psutil.cpu_percent')
    @patch('psutil.virtual_memory')
    @patch('psutil.disk_usage')
    def test_dashboard_refresh_and_accessibility(self, mock_disk, mock_memory, mock_cpu):
        # Setup mock system stats
        mock_cpu.return_value = 45.5
        mock_memory.return_value = MagicMock(percent=60.2)
        mock_disk.return_value = MagicMock(percent=75.8)

        # Setup auth
        headers = {'X-Auth-Token': 'test_token'}

        # Mock database connection check
        self.mock_database.is_database_connected.return_value = True

        # Make request
        response = self.client.get('/', headers=headers)
        self.assertEqual(response.status_code, 200)

        html = response.data.decode('utf-8')

        # Check for auto-refresh meta tag
        self.assertIn('<meta http-equiv="refresh" content="60">', html, "Auto-refresh meta tag missing")

        # Check for accessibility attributes on progress bars
        # CPU
        self.assertIn('role="progressbar"', html)
        self.assertIn('aria-label="CPU Usage"', html)
        self.assertIn('aria-valuenow="45.5"', html)

        # Memory
        self.assertIn('aria-label="Memory Usage"', html)
        self.assertIn('aria-valuenow="60.2"', html)

        # Disk
        self.assertIn('aria-label="Disk Usage"', html)
        self.assertIn('aria-valuenow="75.8"', html)

        # Check for hidden status indicators
        self.assertIn('class="status-indicator status-running"', html)
        self.assertIn('aria-hidden="true"', html)

if __name__ == '__main__':
    unittest.main()
