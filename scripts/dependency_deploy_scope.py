"""Fail-closed classifier for dependency-only production rollback eligibility."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_FILES = {"pyproject.toml", "uv.lock"}
ALLOWED_FILES = REQUIRED_FILES | {
    "CHANGELOG.md",
    "README.md",
    "tests/test_dependency_deploy_scope.py",
    "tests/test_dependency_metadata.py",
    "tests/test_deploy_workflow_config.py",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _rejected(reason: str) -> dict[str, object]:
    return {
        "dependency_only": False,
        "base_sha": "",
        "pr_number": 0,
        "reason": reason,
    }


def classify(payload: dict[str, Any]) -> dict[str, object]:
    """Return rollback eligibility only for one exact, migration-free dependency PR."""
    pull_requests = payload.get("pull_requests")
    if not isinstance(pull_requests, list) or len(pull_requests) != 1:
        return _rejected("expected exactly one associated pull request")

    pull_request = pull_requests[0]
    if not isinstance(pull_request, dict):
        return _rejected("associated pull request payload is malformed")

    repository = payload.get("repository")
    head_sha = payload.get("head_sha")
    base_ref = payload.get("base_ref")
    base = pull_request.get("base")
    head = pull_request.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        return _rejected("pull request refs are malformed")
    head_repository = head.get("repo")
    if not isinstance(head_repository, dict):
        return _rejected("pull request repository is missing")

    if not pull_request.get("merged_at"):
        return _rejected("associated pull request is not merged")
    if pull_request.get("merge_commit_sha") != head_sha:
        return _rejected("pull request merge commit does not match the deployed commit")
    if base.get("ref") != base_ref:
        return _rejected("pull request targets a different deployment branch")
    if head_repository.get("full_name") != repository:
        return _rejected("pull request originates from a different repository")

    base_sha = base.get("sha")
    if not isinstance(base_sha, str) or SHA_PATTERN.fullmatch(base_sha) is None:
        return _rejected("pull request base SHA is invalid")
    if not isinstance(head_sha, str) or SHA_PATTERN.fullmatch(head_sha) is None:
        return _rejected("deployed commit SHA is invalid")

    files = pull_request.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        return _rejected("pull request file list is malformed")
    changed_files = set(files)
    if not changed_files >= REQUIRED_FILES:
        return _rejected("dependency rollback requires both pyproject.toml and uv.lock")
    unexpected = sorted(changed_files - ALLOWED_FILES)
    if unexpected:
        return _rejected(f"unexpected runtime-capable files: {', '.join(unexpected)}")

    pr_number = pull_request.get("number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        return _rejected("pull request number is invalid")

    return {
        "dependency_only": True,
        "base_sha": base_sha,
        "pr_number": pr_number,
        "reason": "eligible dependency-only pull request",
    }


def _write_github_outputs(path: Path, result: dict[str, object]) -> None:
    reason = json.dumps(result["reason"], ensure_ascii=True)
    lines = [
        f"dependency_only={str(result['dependency_only']).lower()}",
        f"base_sha={result['base_sha']}",
        f"pr_number={result['pr_number']}",
        f"reason={reason}",
    ]
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = _rejected(f"scope input could not be read: {type(exc).__name__}")
    else:
        result = classify(payload) if isinstance(payload, dict) else _rejected("scope input must be a JSON object")

    if args.github_output is not None:
        _write_github_outputs(args.github_output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
