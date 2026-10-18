"""Fail closed when an installed production environment drifts from uv.lock."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform as python_platform
import re
import sys
import tomllib
from collections import defaultdict, deque
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
    if marker == "sys_platform == 'win32'":
        return platform == "win32"
    if marker == "platform_python_implementation != 'PyPy'":
        return python_platform.python_implementation() != "PyPy"
    if marker == "implementation_name != 'PyPy'":
        return sys.implementation.name != "PyPy"
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
    *,
    include_dev: bool = False,
) -> list[str]:
    """Compare the installed set with the active root, transitive and extra edges.

    Production is the default used by the image smoke. Canary installations use
    uv's default dev group and must explicitly select it with ``include_dev``.
    Unknown markers and ambiguous/version-conflicting edges fail closed.
    """
    errors = verify_lock_sources(lock)
    packages = lock.get("package", [])
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    projects: list[dict[str, Any]] = []
    for package in packages:
        name = _canonicalize(str(package["name"]))
        source = package.get("source", {})
        if source.get("virtual") == ".":
            projects.append(package)
            continue
        by_name[name].append(package)

    if len(projects) != 1:
        return errors + [f"uv.lock must contain exactly one virtual root project, found {len(projects)}"]

    pending = deque((edge, "direct production dependency") for edge in projects[0].get("dependencies", []))
    if include_dev:
        pending.extend(
            (edge, "direct development dependency") for edge in projects[0].get("dev-dependencies", {}).get("dev", [])
        )
    selected: dict[str, tuple[dict[str, Any], str]] = {}
    visited_extras: dict[str, set[str]] = defaultdict(set)
    while pending:
        dependency, origin = pending.popleft()
        name = _canonicalize(str(dependency["name"]))
        try:
            if not _marker_applies(dependency.get("marker"), platform):
                continue
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue

        candidates = [
            package
            for package in by_name.get(name, [])
            if ("version" not in dependency or str(package["version"]) == str(dependency["version"]))
            and ("source" not in dependency or package["source"] == dependency["source"])
        ]
        if len(candidates) != 1:
            errors.append(f"{name}: dependency edge resolves to {len(candidates)} locked packages")
            continue
        package = candidates[0]
        if name in selected:
            if selected[name][0] is not package:
                errors.append(f"{name}: dependency graph selects conflicting locked packages")
                continue
        else:
            selected[name] = (package, origin)
            pending.extend((edge, "locked dependency") for edge in package.get("dependencies", []))

        optional = {_canonicalize(extra): edges for extra, edges in package.get("optional-dependencies", {}).items()}
        for extra in dependency.get("extra", []):
            extra = _canonicalize(extra)
            if extra in visited_extras[name]:
                continue
            visited_extras[name].add(extra)
            if extra not in optional:
                errors.append(f"{name}: requested extra is absent from uv.lock: {extra}")
                continue
            pending.extend((edge, "locked dependency") for edge in optional[extra])

    installed = {_canonicalize(name): version for name, version in installed_versions.items()}
    for name, (_, origin) in sorted(selected.items()):
        if name not in installed:
            errors.append(f"{name}: {origin} is not installed")

    for name, version in sorted(installed.items()):
        if name not in by_name:
            errors.append(f"{name}: installed package is absent from uv.lock")
        elif name not in selected:
            errors.append(f"{name}: installed package is outside the selected dependency graph")
        elif version != str(selected[name][0]["version"]):
            errors.append(f"{name}: installed {version}, locked {selected[name][0]['version']}")
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
        "--include-dev", action="store_true", help="also require the dev dependency group installed by default uv sync"
    )
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help="validate trusted package sources without inspecting the active environment",
    )
    args = parser.parse_args(argv)
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    errors = (
        verify_lock_sources(lock)
        if args.lock_only
        else verify_environment(lock, _installed_versions(), sys.platform, include_dev=args.include_dev)
    )
    print(json.dumps({"status": "passed" if not errors else "failed", "errors": errors}, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
