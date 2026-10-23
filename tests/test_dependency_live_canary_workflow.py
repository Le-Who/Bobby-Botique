"""Security contracts for the protected manual dependency live canary."""

import json
import shlex
from pathlib import Path

import pytest
import yaml

from scripts import dependency_environment_check

WORKFLOW_PATH = Path(".github/workflows/dependency-live-canary.yml")


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_live_canary_is_manual_protected_and_never_runs_on_pull_request_code_implicitly() -> None:
    workflow = _workflow()

    assert "workflow_dispatch:" in workflow
    assert "pr_number:" in workflow
    assert "pull_request:" not in workflow
    assert "environment: dependency-canary" in workflow
    assert "continue-on-error" not in workflow


def test_preflight_rejects_forks_wrong_base_and_non_dependency_files() -> None:
    workflow = _workflow()
    preflight = workflow.split("  preflight:", 1)[1].split("  validate-lock:", 1)[0]

    assert "pull-requests: read" in preflight
    assert "pr.head.repo.full_name" in preflight
    assert "repository.default_branch" in preflight
    assert "github.rest.pulls.listFiles" in preflight
    assert 'new Set(["pyproject.toml", "uv.lock"])' in preflight
    assert "unexpected" in preflight


def test_candidate_lock_is_validated_without_credentials_before_protected_job() -> None:
    workflow = _workflow()
    validation_job = workflow.split("  validate-lock:", 1)[1].split("  canary:", 1)[0]

    assert "environment:" not in validation_job
    assert "secrets." not in validation_job
    assert validation_job.count("persist-credentials: false") == 2
    assert "--lock-only" in validation_job
    assert validation_job.count("uv sync --locked --no-build --link-mode copy") == 2
    assert "baseline/.venv/bin/python baseline/scripts/dependency_environment_check.py" in validation_job
    assert "candidate/.venv/bin/python baseline/scripts/dependency_environment_check.py" in validation_job


@pytest.mark.parametrize("job_name", ["validate-lock", "canary"])
def test_installed_canary_commands_require_dev_dependencies(job_name, tmp_path, monkeypatch, capsys) -> None:
    """Execute the checker arguments selected by each real installation step."""
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        '[[package]]\nname = "sample-app"\nversion = "0"\nsource = { virtual = "." }\n'
        '[package.dev-dependencies]\ndev = [{ name = "required-dev-tool" }]\n'
        '[[package]]\nname = "required-dev-tool"\nversion = "1"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(dependency_environment_check, "_installed_versions", dict)
    workflow = yaml.safe_load(_workflow())
    commands = [
        shlex.split(line)
        for step in workflow["jobs"][job_name]["steps"]
        for line in step.get("run", "").splitlines()
        if "dependency_environment_check.py" in line and "--lock-only" not in line
    ]

    assert len(commands) == 2
    for command in commands:
        arguments = command[2:]
        arguments[arguments.index("--lock") + 1] = str(lock_path)
        assert dependency_environment_check.main(arguments) == 1
        assert json.loads(capsys.readouterr().out) == {
            "status": "failed",
            "errors": ["required-dev-tool: direct development dependency is not installed"],
        }


def test_protected_candidate_install_has_no_credentials_or_secrets_and_uses_trusted_script() -> None:
    workflow = _workflow()
    canary_job = workflow.split("  canary:", 1)[1]
    install_block = canary_job.split("- name: Sync baseline and candidate locks", 1)[1].split(
        "- name: Run controlled baseline and candidate probes", 1
    )[0]

    assert 'python -m pip install "uv==0.13.5"' in canary_job
    assert canary_job.count("persist-credentials: false") == 2
    assert install_block.count("uv sync --locked --no-build --link-mode copy") == 2
    assert "--lock-only" in install_block
    assert "baseline/.venv/bin/python baseline/scripts/dependency_environment_check.py" in install_block
    assert "candidate/.venv/bin/python baseline/scripts/dependency_environment_check.py" in install_block
    assert "secrets." not in install_block
    assert "candidate/scripts/dependency_live_canary.py" not in canary_job
    assert canary_job.count("baseline/scripts/dependency_live_canary.py") == 3


def test_live_secrets_are_dedicated_and_scoped_to_the_probe_step() -> None:
    workflow = _workflow()
    probe_step = workflow.split("- name: Run controlled baseline and candidate probes", 1)[1].split(
        "- name: Upload redacted canary evidence", 1
    )[0]

    for secret in (
        "CANARY_TELEGRAM_BOT_TOKEN",
        "CANARY_TELEGRAM_CHAT_ID",
        "CANARY_GEMINI_API_KEY",
        "CANARY_TAVILY_API_KEY",
    ):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in probe_step
        assert workflow.count(f"secrets.{secret}") == 1
    assert "DATABASE_URL" not in probe_step
    assert "ADMIN_SECRET" not in probe_step


def test_live_canary_status_and_job_fail_closed() -> None:
    workflow = _workflow()

    assert "statuses: write" in workflow
    assert 'state: "pending"' in workflow
    assert 'const passed = exitCode === 0 && process.env.EVIDENCE_OUTCOME === "success";' in workflow
    assert 'state: passed ? "success" : "failure"' in workflow
    assert "ready_for_review" in workflow
    assert 'exit "${{ steps.canary.outputs.exit_code }}"' in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "if-no-files-found: error" in workflow
