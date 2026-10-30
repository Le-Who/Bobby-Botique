"""Real Chromium controls and stage diagnostics with synthetic HTTP responses."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.browser
@pytest.mark.parametrize("surface", ["queues", "operations"])
def test_queue_revision_controls_and_operations_evidence_in_chromium(tmp_path, surface):
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    if not node or not (browser_root / "node_modules/playwright").is_dir():
        if os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required queue controls browser test needs existing Node/Playwright dependencies")
        pytest.skip("Node/Playwright dependencies are unavailable")
    templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    template = "controls.html" if surface == "queues" else "dashboard.html"
    paths = [
        "js/admin-api.js",
        "css/controls.css",
        "css/style.css",
        "css/dashboard-operations.css",
        "js/controls.js",
        "js/controls-navigation.js",
        "js/controls-queues.js",
        "js/dashboard-operations.js",
    ]
    payload = {
        "surface": surface,
        "html": templates.get_template(template).render(
            csrf_token="synthetic-csrf",
            g=SimpleNamespace(csp_nonce="offline"),
            url_for=lambda endpoint, **values: f"/static/{values['filename']}" if endpoint == "static" else "/logout",
        ),
        "assets": {
            f"/static/{name}": (ROOT / "app/static" / name).read_text(encoding="utf-8")
            for name in paths
            if (ROOT / "app/static" / name).exists()
        },
        "playwright_module": (browser_root / "node_modules/playwright").as_posix(),
        "screenshots": tmp_path.as_posix(),
    }
    result = subprocess.run(
        [node, str(browser_root / "queue_controls_probe.cjs")],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
