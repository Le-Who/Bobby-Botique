"""Contracts for reproducible project dependency metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
LOCKFILE = ROOT / "uv.lock"

EXPECTED_PRODUCTION = {
    "asyncpg",
    "cachetools",
    "cryptography",
    "ephem",
    "geonamescache",
    "google-genai",
    "httpx",
    "hypercorn",
    "msgspec",
    "orjson",
    "pillow",
    "psutil",
    "pydantic",
    "pypdf",
    "python-docx",
    "python-telegram-bot",
    "quart",
    "redis",
    "rich",
    "structlog",
    "tzdata",
    "uvloop",
}

EXPECTED_DEV = {
    "mypy",
    "packaging",
    "pip-audit",
    "pre-commit",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-timeout",
    "pytest-xdist",
    "python-dotenv",
    "ruff",
}


def _metadata() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _names(requirements: list[str]) -> list[str]:
    return [canonicalize_name(Requirement(item).name) for item in requirements]


def test_pyproject_is_the_single_dependency_manifest() -> None:
    metadata = _metadata()
    project = metadata["project"]
    dependency_groups = metadata["dependency-groups"]

    assert project["requires-python"] == ">=3.14,<3.15"
    assert set(_names(project["dependencies"])) == EXPECTED_PRODUCTION
    assert set(_names(dependency_groups["dev"])) == EXPECTED_DEV
    assert not (ROOT / "requirements.txt").exists()
    assert not (ROOT / "requirements-dev.txt").exists()


def test_direct_dependency_names_are_unique_across_scopes() -> None:
    metadata = _metadata()
    production = _names(metadata["project"]["dependencies"])
    development = _names(metadata["dependency-groups"]["dev"])

    assert len(production) == len(set(production))
    assert len(development) == len(set(development))
    assert set(production).isdisjoint(development)


def test_uv_and_frontier_policy_are_explicit() -> None:
    metadata = _metadata()

    assert metadata["tool"]["uv"]["package"] is False
    assert metadata["tool"]["uv"]["required-version"] == "==0.12.6"
    assert metadata["tool"]["dependency-frontier"] == {
        "python-version": "3.14",
        "production-platform": "x86_64-manylinux_2_28",
        "resolution-platforms": ["x86_64-manylinux_2_28", "x86_64-pc-windows-msvc"],
        "cooldown-days": 7,
        "allowed-source-builds": [],
        "license-denylist": [],
    }


def test_uv_lock_contains_a_real_dependency_graph() -> None:
    lock = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))
    packages = lock.get("package", [])
    locked_names = {canonicalize_name(package["name"]) for package in packages}

    assert len(packages) > len(EXPECTED_PRODUCTION)
    assert EXPECTED_PRODUCTION - {"uvloop"} <= locked_names
