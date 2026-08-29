"""Fail closed when an installed production environment drifts from uv.lock."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

APPROVED_REGISTRY = "https://pypi.org/simple"


def _canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _marker_applies(marker: str | None, platform: str) -> bool:
    if marker is None:
        return True
    if marker == "sys_platform != 'win32'":
        return platform != "win32"
    raise ValueError(f"unsupported lock marker for container verification: {marker}")


def verify_lock_sources(lock: dict[str, Any]) -> list[str]:
    """Reject dependency sources that could execute unreviewed build or repository code."""
    errors: list[str] = []
    for package in lock.get("package", []):
        name = _canonicalize(str(package.get("name", "<unnamed>")))
        source = package.get("source")
        if not isinstance(source, dict) or not source:
            errors.append(f"{name}: lock source is missing or malformed")
            continue
        if source.get("virtual") == ".":
            continue
        registry = source.get("registry")
        if registry == APPROVED_REGISTRY and set(source) == {"registry"}:
            continue
        if registry is not None:
            errors.append(f"{name}: unapproved registry {registry}")
            continue
        source_types = ", ".join(sorted(str(key) for key in source))
        errors.append(f"{name}: lock source must be the approved registry, found {source_types}")
    return errors


def verify_environment(
    lock: dict[str, Any],
    installed_versions: dict[str, str],
    platform: str,
) -> list[str]:
    source_errors = verify_lock_sources(lock)
    packages = lock.get("package", [])
    locked_versions: dict[str, set[str]] = {}
    projects: list[dict[str, Any]] = []
    for package in packages:
        name = _canonicalize(str(package["name"]))
        source = package.get("source", {})
        if source.get("virtual") == ".":
            projects.append(package)
            continue
        locked_versions.setdefault(name, set()).add(str(package["version"]))

    if len(projects) != 1:
        return [f"uv.lock must contain exactly one virtual root project, found {len(projects)}"]

    installed = {_canonicalize(name): version for name, version in installed_versions.items()}
    errors: list[str] = list(source_errors)
    for dependency in projects[0].get("dependencies", []):
        name = _canonicalize(str(dependency["name"]))
        try:
            applies = _marker_applies(dependency.get("marker"), platform)
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if applies and name not in installed:
            errors.append(f"{name}: direct production dependency is not installed")

    for name, version in sorted(installed.items()):
        expected = locked_versions.get(name)
        if expected is None:
            errors.append(f"{name}: installed package is absent from uv.lock")
        elif version not in expected:
            errors.append(f"{name}: installed {version}, locked {', '.join(sorted(expected))}")
    return errors


def _installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            versions[_canonicalize(name)] = distribution.version
    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help="validate trusted package sources without inspecting the active environment",
    )
    args = parser.parse_args(argv)
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    errors = (
        verify_lock_sources(lock) if args.lock_only else verify_environment(lock, _installed_versions(), sys.platform)
    )
    print(json.dumps({"status": "passed" if not errors else "failed", "errors": errors}, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
