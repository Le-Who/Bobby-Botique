import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Ensure app can be imported
sys.path.append(os.getcwd())

class TestDashboardUX(unittest.TestCase):
    def setUp(self):
        # We need to ensure clean imports because of the singleton nature of some modules
        import importlib

        # Mock dependencies that cause import errors in this environment
        if 'app.config' not in sys.modules:
            sys.modules['app.config'] = MagicMock()
        if 'app.database' not in sys.modules:
            sys.modules['app.database'] = MagicMock()
        if 'app.metrics' not in sys.modules:
            sys.modules['app.metrics'] = MagicMock()

        # Mock app.utils.time which might be imported
        mock_time_utils = MagicMock()
        if 'app.utils.time' not in sys.modules:
            sys.modules['app.utils.time'] = mock_time_utils
            sys.modules['app.utils'] = MagicMock()
            sys.modules['app.utils'].time = mock_time_utils

        # Mock app.web dependencies specifically
        # We need to patch app.web imports BEFORE importing it
        self.patches = [
            patch.dict(sys.modules, {
                'app.database': MagicMock(),
                'app.config': MagicMock(),
                'app.metrics': MagicMock(),
                'app.request_context': MagicMock(),
                'app.tracing': MagicMock(),
                'psutil': MagicMock()  # Mock psutil for system stats
            })
        ]

        for p in self.patches:
            p.start()

        # Force reload app.web to pick up mocks
        if 'app.web' in sys.modules:
            importlib.reload(sys.modules['app.web'])
        else:
            import app.web

        self.app = sys.modules['app.web'].flask_app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        # Mock authentication
        self.auth_headers = {'X-Auth-Token': 'test_token'}

        # Patch the secrets check in app.web.require_auth (or where it's used)
        # Since require_auth is a decorator applied at import time, we need to mock the environment variable
        # OR mock the settings object that require_auth uses.
        # But require_auth reads os.environ directly or settings.TELEGRAM_BOT_TOKEN
        # Let's mock os.environ
        self.env_patch = patch.dict(os.environ, {'ADMIN_SECRET': 'test_token'})
        self.env_patch.start()

        # Mock database connection check
        self.db_patch = patch('app.database.is_database_connected', return_value=True)
        self.db_patch.start()

        # Mock psutil calls inside the view
        # Since we mocked psutil in sys.modules, we need to configure that mock
        self.psutil_mock = sys.modules['psutil']
        self.psutil_mock.cpu_percent.return_value = 45.5
        self.psutil_mock.virtual_memory.return_value.percent = 60.2
        self.psutil_mock.disk_usage.return_value.percent = 75.8


    def tearDown(self):
        self.db_patch.stop()
        self.env_patch.stop()
        for p in self.patches:
            p.stop()

    def test_dashboard_ux_improvements(self):
        """Test for presence of UX and accessibility improvements in status.html"""
        response = self.client.get('/', headers=self.auth_headers)
        self.assertEqual(response.status_code, 200)

        html = response.data.decode('utf-8')

        # 1. Check for Auto-Refresh Meta Tag
        # This is expected to FAIL initially
        self.assertIn('<meta http-equiv="refresh" content="30">', html,
                      "Missing auto-refresh meta tag for dashboard monitoring")

        # 2. Check for Accessibility Attributes on Progress Bars
        # We expect 3 progress bars (CPU, Memory, Disk)
        # They should have role="progressbar" and aria-valuenow

        # Check CPU Bar
        self.assertIn('role="progressbar"', html, "Missing role='progressbar'")
        self.assertIn('aria-label="CPU Usage"', html, "Missing aria-label for CPU")
        self.assertIn('aria-valuenow="45.5"', html, "Missing aria-valuenow for CPU")

        # Check Memory Bar
        self.assertIn('aria-label="Memory"', html, "Missing aria-label for Memory")
        self.assertIn('aria-valuenow="60.2"', html, "Missing aria-valuenow for Memory")

        # Check Disk Bar
        self.assertIn('aria-label="Disk Storage"', html, "Missing aria-label for Disk")
        self.assertIn('aria-valuenow="75.8"', html, "Missing aria-valuenow for Disk")

        # 3. Check for aria-hidden on decorative status indicators
        # We look for the class and the attribute
        # Expected: <span class="status-indicator status-running" aria-hidden="true"></span>
        self.assertRegex(html, r'class="status-indicator[^"]*" aria-hidden="true"',
                         "Status indicators should be hidden from screen readers")

if __name__ == '__main__':
    unittest.main()
