import pytest
import os
from jinja2 import Environment, FileSystemLoader

@pytest.fixture
def render_template():
    # Setup Jinja2 environment
    template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app', 'templates')
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template('status.html')

    def _render(system_status):
        # Mock status data
        status = {
            "system": system_status,
            "bot": "running",
            "database": "connected",
            "environment": "production",
            "timestamp": "2024-01-01 12:00:00",
            "version": "1.0.0"
        }

        # Mock 'url_for' since it's a Flask function
        def mock_url_for(endpoint, filename=None):
            return f"/static/{filename}" if filename else endpoint

        return template.render(status=status, url_for=mock_url_for)

    return _render

def test_status_colors_normal(render_template):
    """Test that normal usage (<= 75%) does not trigger warning/danger colors."""
    html = render_template({
        "cpu_percent": 50.0,
        "memory_percent": 60.0,
        "disk_percent": 70.0
    })

    # Check that warning (Amber 500) and danger (Red 500) colors are NOT present
    assert "#ef4444" not in html, "Danger color found unexpectedly"
    assert "#f59e0b" not in html, "Warning color found unexpectedly"

def test_status_colors_warning(render_template):
    """Test that warning usage (> 75% and <= 90%) triggers warning color."""
    html = render_template({
        "cpu_percent": 80.0,
        "memory_percent": 80.0,
        "disk_percent": 80.0
    })

    # Check that warning color (Amber 500) IS present
    assert "#f59e0b" in html, "Warning color missing for > 75% usage"
    # Check that danger color (Red 500) is NOT present
    assert "#ef4444" not in html, "Danger color found unexpectedly for warning usage"

def test_status_colors_danger(render_template):
    """Test that danger usage (> 90%) triggers danger color."""
    html = render_template({
        "cpu_percent": 95.0,
        "memory_percent": 95.0,
        "disk_percent": 95.0
    })

    # Check that danger color (Red 500) IS present
    assert "#ef4444" in html, "Danger color missing for > 90% usage"
    # Check that warning color (Amber 500) is NOT present (danger takes precedence)
    assert "#f59e0b" not in html, "Warning color found unexpectedly for danger usage"

def test_status_accessibility_preserved(render_template):
    """Ensure basic accessibility attributes are still present."""
    html = render_template({
        "cpu_percent": 50.0,
        "memory_percent": 50.0,
        "disk_percent": 50.0
    })

    assert 'role="progressbar"' in html
    assert 'aria-label="CPU Usage"' in html
