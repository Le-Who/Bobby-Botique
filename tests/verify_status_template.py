from jinja2 import Environment, FileSystemLoader
import os

def test_status_template_accessibility():
    # Setup Jinja2 environment
    template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app', 'templates')
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template('status.html')

    # Mock data with specific values to verify rendering
    status = {
        "system": {
            "cpu_percent": 45.5,
            "memory_percent": 60.2,
            "disk_percent": 80.1
        },
        "bot": "running",
        "database": "connected",
        "environment": "production",
        "timestamp": "2024-01-01 12:00:00",
        "version": "1.0.0"
    }

    # Mock 'url_for' since it's a Flask function
    def mock_url_for(endpoint, filename=None):
        return f"/static/{filename}" if filename else endpoint

    rendered = template.render(status=status, url_for=mock_url_for)

    print("Verifying template accessibility features...")

    # 1. Check for Auto-Refresh Meta Tag
    if '<meta http-equiv="refresh" content="60">' in rendered:
        print("✅ Meta refresh tag found.")
    else:
        print("❌ Meta refresh tag MISSING.")

    # 2. Check for CPU Progress Bar ARIA
    # We look for the container with role="progressbar" and correct values
    if 'role="progressbar"' in rendered:
        print("✅ role='progressbar' found.")
    else:
        print("❌ role='progressbar' MISSING.")

    # Specific check for CPU
    if 'aria-label="CPU Usage"' in rendered and 'aria-valuenow="45.5"' in rendered:
         print("✅ CPU Usage ARIA attributes correct.")
    else:
         print("❌ CPU Usage ARIA attributes MISSING or INCORRECT.")

    # 3. Check for Memory Progress Bar ARIA
    if 'aria-label="Memory Usage"' in rendered and 'aria-valuenow="60.2"' in rendered:
         print("✅ Memory Usage ARIA attributes correct.")
    else:
         print("❌ Memory Usage ARIA attributes MISSING or INCORRECT.")

    # 4. Check for Disk Progress Bar ARIA
    if 'aria-label="Disk Storage"' in rendered and 'aria-valuenow="80.1"' in rendered:
         print("✅ Disk Storage ARIA attributes correct.")
    else:
         print("❌ Disk Storage ARIA attributes MISSING or INCORRECT.")

    # 5. Check for Status Indicators (aria-hidden)
    # We expect the span to have aria-hidden="true"
    if 'class="status-indicator status-running" aria-hidden="true"' in rendered or \
       'class="status-indicator status-running" aria-hidden=\'true\'' in rendered:
        print("✅ Status indicator aria-hidden='true' found.")
    else:
        print("❌ Status indicator aria-hidden='true' MISSING.")

if __name__ == "__main__":
    try:
        test_status_template_accessibility()
    except Exception as e:
        print(f"❌ Error running verification: {e}")
