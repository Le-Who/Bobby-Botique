"""Contract tests for truthful repository verification in GitHub Actions."""

from pathlib import Path

CI_WORKFLOW = Path(".github/workflows/ci.yml")


def _workflow() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _job(text: str, name: str, next_name: str | None = None) -> str:
    block = text.split(f"  {name}:", 1)[1]
    if next_name is not None:
        block = block.split(f"  {next_name}:", 1)[0]
    return block


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

    assert 'python -m pip install "uv==0.12.6"' in workflow
    assert workflow.count("uv sync --locked") == 5
    assert "-r requirements.txt" not in workflow
    assert "requirements-dev.txt" not in workflow


def test_ci_lint_job_enforces_locked_ruff_formatting() -> None:
    lint_job = _job(_workflow(), "lint", "type-check")

    assert "uv run --locked ruff check ." in lint_job
    assert "uv run --locked ruff format --check ." in lint_job


def test_ci_separates_unit_and_integration_suites() -> None:
    workflow = _workflow()
    unit_job = _job(workflow, "test-unit", "test-integration")
    integration_job = _job(workflow, "test-integration")

    assert 'uv run --locked pytest tests/ --ignore=tests/integration -m "not integration"' in unit_job
    assert "--ignore=tests/integration" in unit_job
    assert 'uv run --locked pytest -m "integration"' in integration_job
    assert "-n 0" in integration_job


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


def test_ci_gates_application_types_and_production_dependencies() -> None:
    workflow = _workflow()
    type_job = _job(workflow, "type-check", "test-unit")

    assert "uv run --locked mypy app bot.py" in type_job
    assert "pip-audit" in workflow
    assert "uv export --locked --no-dev" in workflow
    assert "production-requirements.txt" in workflow
    assert "uv run --locked pip-audit" in workflow
