"""Behavioral tests for dependency-only production rollback classification."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dependency_deploy_scope.py"
CANDIDATE_SHA = "a" * 40
BASE_SHA = "b" * 40


def _payload(*, files: list[str], **overrides: object) -> dict[str, object]:
    pull_request: dict[str, object] = {
        "number": 42,
        "merged_at": "2026-08-29T12:00:00Z",
        "merge_commit_sha": CANDIDATE_SHA,
        "base": {"ref": "vps_testai", "sha": BASE_SHA},
        "head": {"repo": {"full_name": "owner/repo"}},
        "files": files,
    }
    pull_request.update(overrides)
    return {
        "repository": "owner/repo",
        "head_sha": CANDIDATE_SHA,
        "base_ref": "vps_testai",
        "pull_requests": [pull_request],
    }


def _classify(tmp_path: Path, payload: dict[str, object]) -> dict[str, object]:
    input_path = tmp_path / "scope.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(input_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_allows_one_same_repository_dependency_pr_with_non_runtime_review_files(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        _payload(
            files=[
                "pyproject.toml",
                "uv.lock",
                "tests/test_dependency_deploy_scope.py",
                "tests/test_deploy_workflow_config.py",
                "tests/test_dependency_metadata.py",
                "README.md",
                "CHANGELOG.md",
            ]
        ),
    )

    assert result == {
        "dependency_only": True,
        "base_sha": BASE_SHA,
        "pr_number": 42,
        "reason": "eligible dependency-only pull request",
    }


def test_rejects_application_or_migration_changes(tmp_path: Path) -> None:
    result = _classify(tmp_path, _payload(files=["pyproject.toml", "uv.lock", "app/database.py"]))

    assert result["dependency_only"] is False
    assert result["base_sha"] == ""
    assert "app/database.py" in str(result["reason"])


def test_rejects_changes_to_the_classifier_or_deployment_control_plane(tmp_path: Path) -> None:
    for control_file in ("scripts/dependency_deploy_scope.py", ".github/workflows/deploy.yml"):
        result = _classify(tmp_path, _payload(files=["pyproject.toml", "uv.lock", control_file]))

        assert result["dependency_only"] is False
        assert control_file in str(result["reason"])


def test_rejects_scope_without_both_dependency_manifests(tmp_path: Path) -> None:
    result = _classify(tmp_path, _payload(files=["pyproject.toml", "README.md"]))

    assert result["dependency_only"] is False
    assert "pyproject.toml and uv.lock" in str(result["reason"])


def test_rejects_ambiguous_or_unrelated_pull_requests(tmp_path: Path) -> None:
    payload = _payload(files=["pyproject.toml", "uv.lock"])
    payload["pull_requests"] = [
        *payload["pull_requests"],
        {
            **payload["pull_requests"][0],
            "number": 43,
        },
    ]

    result = _classify(tmp_path, payload)

    assert result["dependency_only"] is False
    assert "exactly one" in str(result["reason"])


def test_rejects_wrong_commit_base_or_repository(tmp_path: Path) -> None:
    cases = [
        _payload(files=["pyproject.toml", "uv.lock"], merge_commit_sha="c" * 40),
        _payload(files=["pyproject.toml", "uv.lock"], base={"ref": "main", "sha": BASE_SHA}),
        _payload(files=["pyproject.toml", "uv.lock"], head={"repo": {"full_name": "fork/repo"}}),
    ]

    for payload in cases:
        result = _classify(tmp_path, payload)
        assert result["dependency_only"] is False
        assert result["base_sha"] == ""
