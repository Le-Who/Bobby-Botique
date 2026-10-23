"""Contract tests for truthful repository verification in GitHub Actions."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

CI_WORKFLOW = Path(".github/workflows/ci.yml")
CI_ADVISORY_WORKFLOW = Path(".github/workflows/ci-advisory.yml")


def _workflow() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _advisory_workflow() -> str:
    return CI_ADVISORY_WORKFLOW.read_text(encoding="utf-8")


def _parsed_workflow(path: Path = CI_WORKFLOW) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(path: Path) -> dict:
    # BaseLoader preserves the GitHub Actions `on` key instead of interpreting
    # it as a YAML 1.1 boolean. Job contracts use SafeLoader for real booleans.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["on"]


def _jobs(text: str) -> dict[str, str]:
    return dict(re.findall(r"(?ms)^  ([\w-]+):\n(.*?)(?=^  [\w-]+:\n|\Z)", text.split("jobs:\n", 1)[1]))


def _job(text: str, name: str) -> str:
    return _jobs(text)[name]


def test_ci_verifies_supported_push_branches_and_all_pull_requests() -> None:
    core = _triggers(CI_WORKFLOW)
    advisory = _triggers(CI_ADVISORY_WORKFLOW)

    assert core == advisory
    assert core["push"] == {"branches": ["vps_testai", "main", "TEST_gemaibotv2"]}
    assert core["pull_request"] in ("", {})


def test_ci_advisory_checks_remain_visible_without_becoming_deploy_prerequisites() -> None:
    core = _parsed_workflow()
    advisory = _parsed_workflow(CI_ADVISORY_WORKFLOW)

    assert core["name"] == "CI"
    assert advisory["name"] == "CI Advisory"
    assert set(core["jobs"]) == {
        "lint",
        "type-check",
        "test-unit",
        "test-integration",
        "test-browser",
        "container-smoke",
    }
    assert set(advisory["jobs"]) == {"dependency-audit", "observability-config"}
    for workflow in (core, advisory):
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            assert not job.get("continue-on-error", False)
            assert "if" not in job
            assert not job.get("needs")
            assert all(not step.get("continue-on-error", False) for step in job["steps"])

    deploy = _triggers(Path(".github/workflows/deploy.yml"))
    assert deploy["workflow_run"]["workflows"] == ["CI"]


def test_ci_never_references_removed_load_test_module() -> None:
    assert "load_test.py" not in _workflow()


def test_ci_uses_exact_uv_and_the_committed_lock_in_every_python_job() -> None:
    for path in (CI_WORKFLOW, CI_ADVISORY_WORKFLOW):
        workflow = _parsed_workflow(path)
        python_jobs = {
            name: job
            for name, job in workflow["jobs"].items()
            if any(step.get("uses", "").startswith("actions/setup-python@") for step in job["steps"])
        }
        assert python_jobs
        for name, job in python_jobs.items():
            setup = [step for step in job["steps"] if step.get("uses") == "astral-sh/setup-uv@v7"]
            assert len(setup) == 1, name
            assert setup[0]["with"]["version"] == "0.13.5", name
            assert setup[0]["with"]["enable-cache"] is True, name
            assert setup[0]["with"]["cache-dependency-glob"] == "uv.lock", name
            assert any("uv sync --locked" in step.get("run", "") for step in job["steps"]), name
        text = path.read_text(encoding="utf-8")
        assert "-r requirements.txt" not in text
        assert "requirements-dev.txt" not in text


def test_ci_lint_job_enforces_locked_ruff_formatting() -> None:
    lint_job = _job(_workflow(), "lint")

    assert "uv run --locked ruff check ." in lint_job
    assert "uv run --locked ruff format --check ." in lint_job


def test_ci_separates_unit_and_integration_suites() -> None:
    workflow = _workflow()
    unit_job = _job(workflow, "test-unit")
    integration_job = _job(workflow, "test-integration")

    assert 'uv run --locked pytest tests/ --ignore=tests/integration -m "not integration and not browser"' in unit_job
    assert "--ignore=tests/integration" in unit_job
    assert 'uv run --locked pytest -m "integration"' in integration_job
    assert "-n 0" in integration_job
    assert "needs:" not in integration_job


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


def test_browser_job_discovers_new_nested_and_parametrized_browser_cases(tmp_path) -> None:
    """Execute the workflow selector on a controlled corpus with literal nodeids.

    The required CI browser job separately discovers and runs the real corpus.
    This contract must not import that entire corpus twice within a unit test.
    """
    job = yaml.safe_load(_workflow())["jobs"]["test-browser"]
    run = next(step["run"] for step in job["steps"] if step.get("name") == "Run required browser regressions")
    command = shlex.split(run)
    assert command[:4] == ["uv", "run", "--locked", "pytest"]
    (tmp_path / "pytest.ini").write_text(Path("pytest.ini").read_text(encoding="utf-8"), encoding="utf-8")
    sources = {
        "tests/test_natal_web_report.py": (
            "import pytest\npytestmark = pytest.mark.browser\ndef test_report(): pass\n"
        ),
        "tests/test_compatibility_miniapp.py": (
            "import pytest\n"
            "@pytest.mark.browser\n"
            "class TestCompatibility:\n"
            "    def test_form(self): pass\n"
            "def test_non_browser_helper(): pass\n"
        ),
        "tests/future/test_unfamiliar_feature.py": (
            "import pytest\n"
            "@pytest.mark.browser\n"
            "@pytest.mark.parametrize('mode', ['first', 'second'])\n"
            "def test_new_feature(mode): pass\n"
            "def test_plain(): pass\n"
            "@pytest.mark.integration\n"
            "def test_service(): pass\n"
        ),
    }
    for relative_path, source in sources.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
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
    inventory = tmp_path / "effective.json"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *command[4:], "--collect-only", "-q", "-p", "ci_browser_inventory"],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=25,
        env={**environment, "GEMAIBOT_BROWSER_INVENTORY": str(inventory)},
        check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    effective = set(json.loads(inventory.read_text(encoding="utf-8")))
    expected = {
        "tests/test_natal_web_report.py::test_report",
        "tests/test_compatibility_miniapp.py::TestCompatibility::test_form",
        "tests/future/test_unfamiliar_feature.py::test_new_feature[first]",
        "tests/future/test_unfamiliar_feature.py::test_new_feature[second]",
    }
    assert effective == expected, f"omitted={sorted(expected - effective)}, extra={sorted(effective - expected)}"


def test_ci_selectors_preserve_service_markers_and_exclude_browser_cases_from_unit(tmp_path) -> None:
    """Browser cases leave unit CI; integration and browser keep both markers."""
    (tmp_path / "pytest.ini").write_text(Path("pytest.ini").read_text(encoding="utf-8"), encoding="utf-8")
    sources = {
        "tests/test_cases.py": (
            "import pytest\n"
            "def test_plain(): pass\n"
            "@pytest.mark.unit\n"
            "def test_unit(): pass\n"
            "@pytest.mark.integration\n"
            "def test_database(): pass\n"
            "@pytest.mark.browser\n"
            "def test_browser(): pass\n"
            "@pytest.mark.integration\n"
            "@pytest.mark.browser\n"
            "def test_browser_with_service(): pass\n"
        ),
        "tests/integration/test_database.py": (
            "import pytest\npytestmark = pytest.mark.integration\ndef test_storage(): pass\n"
        ),
        "tests/future/test_new_cases.py": (
            "import pytest\n"
            "class TestNewFeature:\n"
            "    @pytest.mark.parametrize('mode', ['first', 'second'])\n"
            "    def test_plain(self, mode): pass\n"
            "    @pytest.mark.integration\n"
            "    def test_service(self): pass\n"
            "    @pytest.mark.browser\n"
            "    @pytest.mark.parametrize('mode', ['first', 'second'])\n"
            "    def test_page(self, mode): pass\n"
        ),
    }
    for relative_path, source in sources.items():
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    (tmp_path / "ci_partition_inventory.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "def pytest_collection_finish(session):\n"
        "    Path(os.environ['GEMAIBOT_SELECTION_INVENTORY']).write_text(\n"
        "        json.dumps([item.nodeid for item in session.items]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    expected = {
        "test-unit": {
            "tests/test_cases.py::test_plain",
            "tests/test_cases.py::test_unit",
            "tests/future/test_new_cases.py::TestNewFeature::test_plain[first]",
            "tests/future/test_new_cases.py::TestNewFeature::test_plain[second]",
        },
        "test-integration": {
            "tests/test_cases.py::test_database",
            "tests/test_cases.py::test_browser_with_service",
            "tests/integration/test_database.py::test_storage",
            "tests/future/test_new_cases.py::TestNewFeature::test_service",
        },
        "test-browser": {
            "tests/test_cases.py::test_browser",
            "tests/test_cases.py::test_browser_with_service",
            "tests/future/test_new_cases.py::TestNewFeature::test_page[first]",
            "tests/future/test_new_cases.py::TestNewFeature::test_page[second]",
        },
    }
    jobs = _parsed_workflow()["jobs"]
    for job_name, expected_nodeids in expected.items():
        run = next(step["run"] for step in jobs[job_name]["steps"] if "uv run --locked pytest" in step.get("run", ""))
        command = shlex.split(run)
        assert command[:4] == ["uv", "run", "--locked", "pytest"]
        inventory = tmp_path / f"{job_name}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *command[4:],
                "--override-ini=addopts=",
                "-n",
                "0",
                "--collect-only",
                "-q",
                "-p",
                "ci_partition_inventory",
            ],
            cwd=tmp_path,
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env={
                **os.environ,
                "PYTHON_DOTENV_DISABLED": "1",
                "GEMAIBOT_SELECTION_INVENTORY": str(inventory),
                "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
            check=False,
        )
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        effective = set(json.loads(inventory.read_text(encoding="utf-8")))
        assert effective == expected_nodeids, (
            f"{job_name}: omitted={sorted(expected_nodeids - effective)}, extra={sorted(effective - expected_nodeids)}"
        )


def test_ci_gates_application_types() -> None:
    type_job = _job(_workflow(), "type-check")

    assert "uv run --locked mypy app bot.py" in type_job


def test_advisory_retains_production_dependency_evidence_when_audit_fails() -> None:
    job = _parsed_workflow(CI_ADVISORY_WORKFLOW)["jobs"]["dependency-audit"]
    steps = job["steps"]
    runs = [step.get("run", "") for step in steps]
    export_index = next(index for index, run in enumerate(runs) if "uv export --locked --no-dev" in run)
    sbom_index = next(index for index, run in enumerate(runs) if "--format cyclonedx1.5" in run)
    license_index = next(index for index, run in enumerate(runs) if "scripts/dependency_license_inventory.py" in run)
    audit_index = next(index for index, run in enumerate(runs) if "uv run --locked pip-audit" in run)
    upload_index = next(index for index, step in enumerate(steps) if step.get("uses") == "actions/upload-artifact@v4")

    assert export_index < sbom_index < license_index < audit_index < upload_index
    assert "-r .pytest_tmp/production-requirements.txt" in runs[audit_index]
    assert not steps[audit_index].get("continue-on-error", False)
    upload = steps[upload_index]
    assert upload["if"] == "${{ !cancelled() }}"
    assert upload["with"]["if-no-files-found"] == "error"
    assert set(upload["with"]["path"].splitlines()) == {
        ".pytest_tmp/production-requirements.txt",
        ".pytest_tmp/sbom.cdx.json",
        ".pytest_tmp/licenses.json",
    }


def test_ci_builds_and_offline_smokes_the_production_container() -> None:
    container_job = _job(_workflow(), "container-smoke")
    job = _parsed_workflow()["jobs"]["container-smoke"]
    build = next(step for step in job["steps"] if step.get("uses") == "docker/build-push-action@v6")

    assert build["with"]["context"] == "."
    assert build["with"]["load"] is True
    assert build["with"]["push"] is False
    assert build["with"]["tags"] == "${{ env.IMAGE_TAG }}"
    assert build["with"]["cache-from"] == "type=gha,scope=production"
    cache_to = set(build["with"]["cache-to"].split(","))
    assert {"type=gha", "scope=production", "mode=max", "ignore-error=true"} <= cache_to
    assert "docker run --rm --network none" in container_job
    assert "scripts/dependency_container_smoke.py" in container_job
    assert "uv pip check" in container_job
    assert "continue-on-error" not in container_job


def test_advisory_requires_actual_stdout_ingestion_and_protected_datasource_checks() -> None:
    job = _job(_advisory_workflow(), "observability-config")

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
