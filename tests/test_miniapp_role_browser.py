"""Role and model choices preserve literal data through actual DOM clicks."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    "scenario", ["role_apostrophe", "role_active_text", "role_standard", "model_flat", "model_grouped"]
)
def test_miniapp_selection_keeps_data_out_of_event_handler_source(scenario):
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required UI regression needs Node.js")
        pytest.skip("UI regression needs Node.js")
    result = subprocess.run(
        [node, "miniapp_role_probe.cjs"],
        input=json.dumps(
            {"html": (root / "app/templates/miniapp.html").read_text(encoding="utf-8"), "scenario": scenario}
        ),
        cwd=root / "tests/browser",
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    if result.returncode and "Cannot find module 'playwright'" in result.stderr:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") != "1":
            pytest.skip("UI regression needs locked Playwright dependencies")
    assert result.returncode == 0, result.stdout + result.stderr
