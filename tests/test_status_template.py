import unittest
import os
import jinja2

class TestStatusTemplate(unittest.TestCase):
    def setUp(self):
        self.mock_status = {
            "bot": "running",
            "database": "connected",
            "timestamp": "2024-01-01 12:00:00",
            "version": "1.0.0",
            "environment": "production",
            "system": {
                "cpu_percent": 45.5,
                "memory_percent": 60.2,
                "disk_percent": 75.0
            }
        }

        # Ensure we can find the template regardless of where test is run from
        template_dir = os.path.join(os.getcwd(), 'app/templates')
        if not os.path.exists(template_dir):
             # Try relative to this file
             template_dir = os.path.join(os.path.dirname(__file__), '../app/templates')

        self.env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
        # Mock url_for
        self.env.globals['url_for'] = lambda endpoint, **values: f"/static/{values.get('filename', '')}"
        self.template = self.env.get_template('status.html')

    def test_render_template(self):
        rendered_html = self.template.render(status=self.mock_status)

        # Check for Auto-refresh
        self.assertIn('<meta http-equiv="refresh" content="60">', rendered_html)

        # Check for ARIA attributes
        self.assertIn('role="progressbar"', rendered_html)
        self.assertIn('aria-label="CPU Usage"', rendered_html)
        self.assertIn('aria-label="Memory"', rendered_html)
        self.assertIn('aria-label="Disk Storage"', rendered_html)
        self.assertIn('aria-valuenow="45.5"', rendered_html)

if __name__ == '__main__':
    unittest.main()
