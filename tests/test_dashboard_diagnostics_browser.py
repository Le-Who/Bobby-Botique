"""Synthetic browser checks for model evidence and unavailable dashboard sources."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def _browser_probe(tmp_path, surface):
    node = shutil.which("node")
    browser_root = ROOT / "tests/browser"
    if not node or not (browser_root / "node_modules/playwright").is_dir():
        if os.environ.get("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required dashboard regression needs Node.js and tests/browser dependencies")
        pytest.skip("Node.js and tests/browser Playwright dependencies are unavailable")
    templates = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=True)
    payload = {
        "surface": surface,
        "html": templates.get_template(f"{surface}.html").render(
            csrf_token="synthetic-csrf",
            g=SimpleNamespace(csp_nonce="offline"),
            url_for=lambda endpoint, **values: f"/static/{values['filename']}" if endpoint == "static" else "/logout",
        ),
        "assets": {
            f"/static/{path.relative_to(ROOT / 'app/static').as_posix()}": path.read_text(encoding="utf-8")
            for path in (
                ROOT / "app/static/js/controls.js",
                ROOT / "app/static/js/controls-diagnostics.js",
                ROOT / "app/static/js/controls-navigation.js",
                ROOT / "app/static/css/controls.css",
                ROOT / "app/static/css/style.css",
            )
        },
        "playwright_module": (browser_root / "node_modules/playwright").as_posix(),
        "screenshots": tmp_path.as_posix(),
    }
    result = subprocess.run(
        [node, str(browser_root / "dashboard_diagnostics_probe.cjs")],
        input=json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
        capture_output=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.browser
def test_dashboard_clears_unavailable_sources_and_distinguishes_local_admission(tmp_path):
    _browser_probe(tmp_path, "dashboard")


@pytest.mark.browser
def test_model_diagnostics_separates_assignments_usage_and_provider_evidence(tmp_path):
    _browser_probe(tmp_path, "controls")
