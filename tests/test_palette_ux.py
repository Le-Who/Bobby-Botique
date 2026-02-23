import pytest
from flask import Flask, render_template

def test_status_page_visuals():
    """
    Test that the status page renders correctly and check for accessibility/visual improvements.
    """
    app = Flask(__name__, template_folder='../app/templates', static_folder='../app/static')

    # Mock status data with high usage to trigger potential warning colors
    status_data = {
        "bot": "running",
        "database": "connected",
        "timestamp": "2024-01-01 12:00:00",
        "version": "2.1.0",
        "environment": "test",
        "system": {
            "cpu_percent": 95,      # Should trigger Red/Danger if logic exists
            "memory_percent": 80,   # Should trigger Amber/Warning if logic exists
            "disk_percent": 20      # Should remain default
        }
    }

    with app.test_request_context():
        html = render_template('status.html', status=status_data)

        # 1. Verify the page renders at all
        assert "System Health" in html
        assert "95%" in html

        # 2. Check for conditional colors
        # Red (#ef4444) for > 90% (cpu_percent is 95)
        # Note: Template uses 'bg-danger'
        assert "bg-danger" in html

        # Amber (#f59e0b) for > 75% (memory_percent is 80)
        # Note: Template uses 'bg-warning'
        assert "bg-warning" in html
