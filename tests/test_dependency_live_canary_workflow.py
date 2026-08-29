"""Security contracts for the protected manual dependency live canary."""

from pathlib import Path

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
    preflight = workflow.split("  preflight:", 1)[1].split("  canary:", 1)[0]

    assert "pull-requests: read" in preflight
    assert "pr.head.repo.full_name" in preflight
    assert "repository.default_branch" in preflight
    assert "github.rest.pulls.listFiles" in preflight
    assert 'new Set(["pyproject.toml", "uv.lock"])' in preflight
    assert "unexpected" in preflight


def test_candidate_install_has_no_secrets_and_uses_only_the_trusted_canary_script() -> None:
    workflow = _workflow()
    canary_job = workflow.split("  canary:", 1)[1]
    install_block = canary_job.split("- name: Sync baseline and candidate locks", 1)[1].split(
        "- name: Run controlled baseline and candidate probes", 1
    )[0]

    assert 'python -m pip install "uv==0.12.6"' in canary_job
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
