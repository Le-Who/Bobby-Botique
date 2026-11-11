"""Role and model choices preserve literal data through actual DOM clicks."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize(
    "scenario",
    [
        "role_apostrophe",
        "role_active_text",
        "role_standard",
        "model_flat",
        "model_grouped",
        "reasoning_admin",
        "reasoning_default",
        "reasoning_gemini",
        "settings_save_newer_edit",
        "settings_duplicate_save",
        "cloud_late_server",
        "cloud_late_edit",
        "cloud_server_after_edit",
        "cloud_server_after_saved_edit",
        "delete_undo_before_send",
        "delete_stale_undo",
        "delete_flush_stale_undo",
        "delete_flush_error",
    ],
)
def test_miniapp_selection_keeps_data_out_of_event_handler_source(scenario, tmp_path):
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required UI regression needs Node.js")
        pytest.skip("UI regression needs Node.js")
    result = subprocess.run(
        [node, "miniapp_role_probe.cjs"],
        input=json.dumps(
            {
                "html": (root / "app/templates/miniapp.html").read_text(encoding="utf-8"),
                "scenario": scenario,
                "mobile": (tmp_path / "miniapp-reasoning-mobile.png").as_posix(),
            }
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
