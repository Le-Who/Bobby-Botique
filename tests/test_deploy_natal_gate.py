import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


def _bash_executable() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    git = shutil.which("git")
    if os.name == "nt" and git:
        git_bash = Path(git).parent.parent / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    pytest.skip("Bash is required to exercise the deployment shell gate.")


def _run_natal_gate(*, enabled: str = "true", failed_check: str = "") -> subprocess.CompletedProcess[str]:
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    start = workflow.index('            if [ "$(printf \'%s\' "$NATAL_REPORTS_ENABLED"')
    end = workflow.index("            # ── Media Cleanup Cron", start)
    gate = textwrap.dedent(workflow[start:end])
    # Run the real workflow branch, replacing only the external Docker boundary.
    harness = textwrap.dedent(
        r"""
        set -eu
        docker() {
          printf 'docker:%s\n' "$*"
          case "$*" in
            exec\ tg-bot\ python\ /app/scripts/natal_readiness.py*)
              [ "$FAKE_FAILURE" != "readiness" ] ;;
            exec\ tg-bot\ python\ /app/scripts/natal_maintenance.py)
              [ "$FAKE_FAILURE" != "maintenance" ] ;;
            rm\ tg-bot-previous) return 0 ;;
            *) return 64 ;;
          esac
        }
        handle_candidate_failure() {
          printf 'failure:%s\n' "$1"
          exit 1
        }
        """
    )
    env = {name: value for name, value in os.environ.items() if name.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}}
    env.update(
        NATAL_REPORTS_ENABLED=enabled,
        WEBHOOK_URL="https://bot.example.test",
        PREVIOUS_PRESERVED="true",
        FAKE_FAILURE=failed_check,
    )
    return subprocess.run(
        [_bash_executable(), "-c", harness + gate],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("enabled", ["true", "TRUE"])
def test_enabled_natal_gate_checks_readiness_before_maintenance_and_previous_removal(enabled):
    result = _run_natal_gate(enabled=enabled)
    events = [line for line in result.stdout.splitlines() if line.startswith("docker:")]

    assert result.returncode == 0, result.stderr
    assert len(events) == 3
    assert events[0].startswith("docker:exec tg-bot python /app/scripts/natal_readiness.py ")
    readiness_args = events[0].split()[4:]
    for flag in ("--check-config", "--check-storage", "--require-external", "--smoke"):
        assert flag in readiness_args
    for flag, value in (
        ("--reference-fixtures", "/app/docs/natal-reference-fixture.moira-jpl.json"),
        ("--webhook-url", "https://bot.example.test"),
        ("--min-city-count", "30000"),
        ("--max-city-warmup-ms", "3000"),
        ("--max-city-search-ms", "300"),
    ):
        assert readiness_args[readiness_args.index(flag) + 1] == value
    assert "--check-horizons" not in readiness_args
    assert events[1:] == [
        "docker:exec tg-bot python /app/scripts/natal_maintenance.py",
        "docker:rm tg-bot-previous",
    ]


@pytest.mark.parametrize("failed_check", ["readiness", "maintenance"])
def test_failed_natal_gate_uses_candidate_failure_and_preserves_previous_container(failed_check):
    result = _run_natal_gate(failed_check=failed_check)

    assert result.returncode == 1
    assert f"failure:natal chart {failed_check} failed" in result.stdout
    assert "docker:rm tg-bot-previous" not in result.stdout
    if failed_check == "readiness":
        assert "docker:exec tg-bot python /app/scripts/natal_maintenance.py" not in result.stdout


def test_disabled_natal_gate_skips_checks_and_accepts_healthy_candidate():
    result = _run_natal_gate(enabled="false")
    events = [line for line in result.stdout.splitlines() if line.startswith("docker:")]

    assert result.returncode == 0, result.stderr
    assert events == ["docker:rm tg-bot-previous"]
