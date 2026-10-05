"""Contract tests for truthful repository verification in GitHub Actions."""

import re
from pathlib import Path

CI_WORKFLOW = Path(".github/workflows/ci.yml")


def _workflow() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _jobs(text: str) -> dict[str, str]:
    return dict(re.findall(r"(?ms)^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", text.split("jobs:\n", 1)[1]))


def _job(text: str, name: str) -> str:
    return _jobs(text)[name]


def test_ci_verifies_supported_push_branches_and_all_pull_requests() -> None:
    workflow = _workflow()
    push_block = workflow.split("  push:", 1)[1].split("  pull_request:", 1)[0]
    pull_request_block = workflow.split("  pull_request:", 1)[1].split("concurrency:", 1)[0]

    for branch in ("vps_testai", "main", "TEST_gemaibotv2"):
        assert branch in push_block
    assert "branches:" not in pull_request_block


def test_ci_never_references_removed_load_test_module() -> None:
    assert "load_test.py" not in _workflow()


def test_ci_uses_exact_uv_and_the_committed_lock_in_every_python_job() -> None:
    workflow = _workflow()

    python_jobs = {name: body for name, body in _jobs(workflow).items() if "uses: actions/setup-python@" in body}
    assert python_jobs
    for name, body in python_jobs.items():
        assert 'python -m pip install "uv==0.12.6"' in body, name
        assert "uv sync --locked" in body, name
    assert "-r requirements.txt" not in workflow
    assert "requirements-dev.txt" not in workflow


def test_ci_lint_job_enforces_locked_ruff_formatting() -> None:
    lint_job = _job(_workflow(), "lint")

    assert "uv run --locked ruff check ." in lint_job
    assert "uv run --locked ruff format --check ." in lint_job


def test_ci_separates_unit_and_integration_suites() -> None:
    workflow = _workflow()
    unit_job = _job(workflow, "test-unit")
    integration_job = _job(workflow, "test-integration")

    assert 'uv run --locked pytest tests/ --ignore=tests/integration -m "not integration"' in unit_job
    assert "--ignore=tests/integration" in unit_job
    assert 'uv run --locked pytest -m "integration"' in integration_job
    assert "-n 0" in integration_job
    assert "needs: [lint]" in integration_job


def test_ci_integration_job_uses_ephemeral_pgvector_database() -> None:
    integration_job = _job(_workflow(), "test-integration")

    assert "services:" in integration_job
    assert "pgvector/pgvector:" in integration_job
    assert "redis:" in integration_job
    assert "redis:7-alpine" in integration_job
    assert "TEST_DATABASE_URL:" in integration_job
    assert "DATABASE_URL:" in integration_job
    assert 'GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL: "true"' in integration_job
    assert integration_job.count("python scripts/migrate.py") == 3
    assert "python scripts/migrate.py --check" in integration_job
    assert "TEST_DATABASE_URL must be set" in integration_job
    assert 'REDIS_URL: "redis://localhost:6379/0"' in integration_job
    assert 'TEST_REDIS_URL: "redis://localhost:6379/15"' in integration_job
    assert "TEST_REDIS_URL must be set" in integration_job


def test_ci_requires_locked_browser_tests_with_chromium() -> None:
    browser_job = _job(_workflow(), "test-browser")

    assert 'GEMAIBOT_REQUIRE_BROWSER_TESTS: "1"' in browser_job
    assert "npm ci --ignore-scripts" in browser_job
    assert "npx --no-install playwright install --with-deps chromium" in browser_job
    for path in (
        "tests/test_natal_web_report.py",
        "tests/test_daily_2048_frontend.py",
        "tests/test_daily_trivia_frontend.py",
        "tests/test_daily_game_ui.py",
    ):
        assert path in browser_job
    assert "-m browser -n 0" in browser_job
    assert "continue-on-error" not in browser_job


def test_ci_gates_application_types_and_production_dependencies() -> None:
    workflow = _workflow()
    type_job = _job(workflow, "type-check")

    assert "uv run --locked mypy app bot.py" in type_job
    assert "pip-audit" in workflow
    assert "uv export --locked --no-dev" in workflow
    assert "production-requirements.txt" in workflow
    assert "uv run --locked pip-audit" in workflow
    assert "--format cyclonedx1.5" in workflow
    assert "sbom.cdx.json" in workflow
    assert "scripts/dependency_license_inventory.py" in workflow
    assert "licenses.json" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_ci_builds_and_offline_smokes_the_production_container() -> None:
    container_job = _job(_workflow(), "container-smoke")

    assert "docker build" in container_job
    assert "docker run --rm --network none" in container_job
    assert "scripts/dependency_container_smoke.py" in container_job
    assert "uv pip check" in container_job
    assert "continue-on-error" not in container_job
