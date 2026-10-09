"""Contract tests for truthful repository verification in GitHub Actions."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

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
    # Discover every marked browser test, including new modules. A fixed list
    # previously omitted the compatibility form from the required Chromium job.
    assert "uv run --locked pytest tests/ -m browser -n 0" in browser_job
    assert not re.search(r"tests/test_\w+\.py", browser_job)
    assert "-m browser -n 0" in browser_job
    assert '--override-ini="addopts=" --timeout=30' in browser_job
    assert "continue-on-error" not in browser_job


def test_browser_job_selection_matches_all_discovered_browser_modules(tmp_path) -> None:
    """Compare real pytest discovery with the effective workflow selector."""
    job = yaml.safe_load(_workflow())["jobs"]["test-browser"]
    run = next(step["run"] for step in job["steps"] if step.get("name") == "Run required browser regressions")
    command = shlex.split(run)
    assert command[:4] == ["uv", "run", "--locked", "pytest"]
    deadline = time.monotonic() + 25
    (tmp_path / "ci_browser_inventory.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "def pytest_collection_finish(session):\n"
        "    Path(os.environ['GEMAIBOT_BROWSER_INVENTORY']).write_text(\n"
        "        json.dumps([item.nodeid for item in session.items]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "GEMAIBOT_REQUIRE_BROWSER_TESTS": "1",
        "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    inventories = iter([tmp_path / "reference.json", tmp_path / "effective.json"])

    def discovered(arguments: list[str]) -> set[str]:
        inventory = next(inventories)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *arguments, "--collect-only", "-q", "-p", "ci_browser_inventory"],
            capture_output=True,
            encoding="utf-8",
            timeout=max(1, deadline - time.monotonic()),
            env={**environment, "GEMAIBOT_BROWSER_INVENTORY": str(inventory)},
            check=False,
        )
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        return set(json.loads(inventory.read_text(encoding="utf-8")))

    reference = discovered(["tests/", "-m", "browser", "-n", "0", "--override-ini=addopts=", "--timeout=30"])
    effective = discovered(command[4:])
    assert reference, "a missing browser inventory must not pass the CI selection contract"
    assert effective == reference, f"omitted={sorted(reference - effective)}, extra={sorted(effective - reference)}"
    assert {node.split("::", 1)[0] for node in effective} == {node.split("::", 1)[0] for node in reference}


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


def test_ci_requires_actual_stdout_ingestion_and_protected_datasource_checks() -> None:
    job = _job(_workflow(), "observability-config")

    assert "python scripts/generate_observability_private_probe.py" in job
    assert "python scripts/check_observability_stack.py" in job
    assert "--ephemeral --grafana-url http://127.0.0.1:3000" in job
    assert "--private-event-file .pytest_tmp/observability-private-event.json" in job
    assert "GEMAIBOT_REQUIRE_OBSERVABILITY_TESTS=1" in job
    assert "GEMAIBOT_OBSERVABILITY_IS_EPHEMERAL=1" in job
    assert "TEST_OBSERVABILITY_MANIFEST=.pytest_tmp/observability-probe/manifest.json" in job
    assert "pytest tests/observability/test_collector_e2e.py" in job
    assert '-m integration -n 0 --override-ini="addopts=" --timeout=30' in job
    assert "trap cleanup_observability EXIT" in job
    assert "continue-on-error" not in job
