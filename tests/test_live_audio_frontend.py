"""Real-page mic ownership with controlled browser device boundaries."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def _run_lifecycle(scenario: str, terminal: str | None = None) -> None:
    node = shutil.which("node")
    if node is None:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required Live Audio regression needs Node.js")
        pytest.skip("Live Audio regression needs Node.js")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "live_audio_lifecycle_probe.cjs"],
        input=json.dumps(
            {
                "scenario": scenario,
                "terminal": terminal,
                "html": (root / "app/templates/live_audio.html").read_text(encoding="utf-8"),
            }
        ),
        cwd=root / "tests/browser",
        capture_output=True,
        encoding="utf-8",
        timeout=25,
    )
    if result.returncode and "Cannot find module 'playwright'" in result.stderr:
        if os.getenv("GEMAIBOT_REQUIRE_BROWSER_TESTS") != "1":
            pytest.skip("Live Audio regression needs locked Playwright dependencies")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", ["permission_denied", "context_failure", "worklet_failure"])
def test_mic_startup_failure_restores_idle_and_releases_acquired_resources(scenario):
    _run_lifecycle(scenario)


@pytest.mark.parametrize("scenario", ["stop_pending_permission", "stop_pending_worklet", "end_pending_permission"])
def test_cancelled_start_cannot_activate_mic_after_the_browser_promise_resolves(scenario):
    _run_lifecycle(scenario)


def test_stop_releases_mic_once_and_keeps_the_live_session_available():
    _run_lifecycle("normal_stop")


def test_previous_pending_capture_cannot_overwrite_a_new_recording():
    _run_lifecycle("superseded_start")


@pytest.mark.parametrize("phase", ["permission", "worklet"])
@pytest.mark.parametrize("terminal", ["model_unavailable", "misconfigured", "server_capacity", "connect_failed"])
def test_terminal_or_fallback_event_cancels_the_pending_capture_before_late_resolution(phase, terminal):
    _run_lifecycle(f"terminal_pending_{phase}", terminal)
