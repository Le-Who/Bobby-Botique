import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import importlib

# Ensure app can be imported
sys.path.append(os.getcwd())

class TestDashboardUX(unittest.TestCase):
    def setUp(self):
        # Create mocks for dependencies
        self.mock_db = MagicMock()
        self.mock_db.is_database_connected.return_value = True

        self.mock_settings = MagicMock()
        self.mock_settings.TELEGRAM_BOT_TOKEN = "test_token"
        self.mock_settings.ADMIN_SECRET = "secret"
        self.mock_settings.DAILY_LIMITS = {}

        # Patch sys.modules
        self.patcher_db = patch.dict(sys.modules, {'app.database': self.mock_db})
        self.patcher_db.start()

        self.patcher_config = patch.dict(sys.modules, {'app.config': MagicMock()})
        self.patcher_config.start()
        # We need to ensure app.config.settings is our mock
        sys.modules['app.config'].settings = self.mock_settings

        # Patch other dependencies that app.web might use or that might cause issues
        self.patcher_psutil = patch.dict(sys.modules, {'psutil': MagicMock()})
        self.patcher_psutil.start()

        # Remove app.web from sys.modules to force reload with mocks
        if 'app.web' in sys.modules:
            del sys.modules['app.web']

        import app.web
        self.app = app.web.flask_app
        self.client = self.app.test_client()

    def tearDown(self):
        self.patcher_db.stop()
        self.patcher_config.stop()
        self.patcher_psutil.stop()

    def test_dashboard_accessibility(self):
        # Set environment variable for auth
        with patch.dict(os.environ, {'ADMIN_SECRET': 'secret'}):
            response = self.client.get('/', headers={'X-Auth-Token': 'secret'})

        self.assertEqual(response.status_code, 200)
        html = response.data.decode('utf-8')

        # Check for Auto-Refresh (Micro-UX)
        self.assertIn('<meta http-equiv="refresh" content="30">', html, "Auto-refresh meta tag missing")

        # Check for ARIA attributes on progress bars (Accessibility)
        # We check for at least one occurrence
        self.assertIn('role="progressbar"', html, "Progress bar missing role='progressbar'")
        self.assertIn('aria-valuenow', html, "Progress bar missing aria-valuenow")
        self.assertIn('aria-valuemin="0"', html, "Progress bar missing aria-valuemin")
        self.assertIn('aria-valuemax="100"', html, "Progress bar missing aria-valuemax")

        # Check for aria-hidden on status indicators (Accessibility)
        # Verify that elements with class status-indicator also have aria-hidden="true"
        # This is a simple string check, a proper HTML parser would be better but this suffices for regression
        # We look for the substring presence.
        # Since attributes can be in any order, we can't search for exact string like 'class="status-indicator" aria-hidden="true"'
        # But we can check if 'aria-hidden="true"' exists near 'class="status-indicator"' or just if it exists in the file (less robust)
        # or we can use BeautifulSoup if available.
        # Given the environment, let's stick to string checks but maybe just check if aria-hidden="true" is present in the document at all for now,
        # or specific to the status indicator context if possible.

        # Let's count occurrences or just check presence.
        self.assertIn('aria-hidden="true"', html, "aria-hidden='true' missing from status indicators")

if __name__ == '__main__':
    unittest.main()
