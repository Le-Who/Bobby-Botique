"""Tests for exact installed-versus-lock verification used by container CI."""

import json
import platform as python_platform
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import dependency_environment_check as checker
from scripts.dependency_environment_check import verify_environment, verify_lock_sources


def _lock() -> dict[str, object]:
    return {
        "package": [
            {
                "name": "demo",
                "version": "1.2.3",
                "source": {"registry": "https://pypi.org/simple"},
                "dependencies": [{"name": "helper"}],
            },
            {"name": "helper", "version": "4.5.6", "source": {"registry": "https://pypi.org/simple"}},
            {
                "name": "sample-app",
                "version": "0.0.0",
                "source": {"virtual": "."},
                "dependencies": [
                    {"name": "demo"},
                    {"name": "uvloop", "marker": "sys_platform != 'win32'"},
                ],
            },
            {"name": "uvloop", "version": "0.22.1", "source": {"registry": "https://pypi.org/simple"}},
        ]
    }


def test_environment_check_accepts_only_exact_locked_versions() -> None:
    assert verify_environment(_lock(), {"demo": "1.2.3", "helper": "4.5.6", "uvloop": "0.22.1"}, "linux") == []


def test_environment_check_handles_platform_markers_without_masking_drift() -> None:
    assert verify_environment(_lock(), {"demo": "1.2.3", "helper": "4.5.6"}, "win32") == []

    errors = verify_environment(
        _lock(),
        {"demo": "1.2.4", "helper": "4.5.6", "unexpected": "9.0"},
        "linux",
    )

    assert "demo: installed 1.2.4, locked 1.2.3" in errors
    assert "uvloop: direct production dependency is not installed" in errors
    assert "unexpected: installed package is absent from uv.lock" in errors


def test_environment_check_fails_closed_for_unknown_markers() -> None:
    lock = _lock()
    project = next(package for package in lock["package"] if package["name"] == "sample-app")
    project["dependencies"].append({"name": "conditional", "marker": "python_version >= '3.14'"})

    assert verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32") == [
        "conditional: unsupported lock marker for container verification: python_version >= '3.14'"
    ]


def test_environment_check_requires_missing_transitive_dependency_from_real_lock() -> None:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    installed = {
        package["name"]: package["version"]
        for package in lock["package"]
        if not package["source"].get("virtual") and package["name"] not in {"uvloop", "h11"}
    }

    assert verify_environment(lock, installed, "win32", include_dev=True) == ["h11: locked dependency is not installed"]


def test_environment_check_traverses_cycles_and_normalizes_package_names() -> None:
    lock = _lock()
    lock["package"][0]["dependencies"] = [{"name": "Help_er"}]
    lock["package"][1]["name"] = "help-er"
    lock["package"][1]["dependencies"] = [{"name": "DEMO"}]

    assert verify_environment(lock, {"DEMO": "1.2.3", "HELP.ER": "4.5.6"}, "win32") == []
    assert verify_environment(lock, {"demo": "1.2.3"}, "win32") == ["help-er: locked dependency is not installed"]


@pytest.mark.parametrize("include_dev", [False, True])
def test_environment_check_requires_dev_graph_only_in_selected_scope(include_dev) -> None:
    lock = _lock()
    lock["package"][2]["dev-dependencies"] = {"dev": [{"name": "pytest"}]}
    lock["package"].append({"name": "pytest", "version": "9.0", "source": {"registry": "https://pypi.org/simple"}})

    errors = verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32", include_dev=include_dev)

    assert errors == (["pytest: direct development dependency is not installed"] if include_dev else [])


def test_production_scope_rejects_installed_dev_only_packages() -> None:
    lock = _lock()
    lock["package"][2]["dev-dependencies"] = {"dev": [{"name": "pytest"}]}
    lock["package"].append({"name": "pytest", "version": "9.0", "source": {"registry": "https://pypi.org/simple"}})
    installed = {"demo": "1.2.3", "helper": "4.5.6", "pytest": "9.0"}

    assert verify_environment(lock, installed, "win32") == [
        "pytest: installed package is outside the selected dependency graph"
    ]
    assert verify_environment(lock, installed, "win32", include_dev=True) == []


@pytest.mark.parametrize("enabled", [False, True])
def test_environment_check_traverses_only_activated_optional_dependencies(enabled) -> None:
    lock = _lock()
    lock["package"][0]["optional-dependencies"] = {"file-cache": [{"name": "filelock"}]}
    lock["package"].append({"name": "filelock", "version": "1.0", "source": {"registry": "https://pypi.org/simple"}})
    if enabled:
        lock["package"][2]["dependencies"][0]["extra"] = ["File_Cache"]

    assert verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32") == (
        ["filelock: locked dependency is not installed"] if enabled else []
    )


def test_environment_check_activates_extra_reached_after_base_package_and_in_a_cycle() -> None:
    lock = _lock()
    lock["package"][0]["optional-dependencies"] = {"job-queue": [{"name": "scheduler"}]}
    lock["package"][1]["dependencies"] = [{"name": "demo", "extra": ["job-queue"]}]
    lock["package"].append(
        {
            "name": "scheduler",
            "version": "3.0",
            "source": {"registry": "https://pypi.org/simple"},
            "dependencies": [{"name": "demo", "extra": ["job-queue"]}],
        }
    )

    assert verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32") == [
        "scheduler: locked dependency is not installed"
    ]
    assert verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6", "scheduler": "3.0"}, "win32") == []


def test_environment_check_fails_closed_for_unresolved_requested_extra() -> None:
    lock = _lock()
    lock["package"][2]["dependencies"][0]["extra"] = ["missing-extra"]

    assert verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32") == [
        "demo: requested extra is absent from uv.lock: missing-extra"
    ]


@pytest.mark.parametrize(
    ("marker", "platform", "implementation", "required"),
    [
        ("sys_platform == 'win32'", "win32", "CPython", True),
        ("sys_platform == 'win32'", "linux", "CPython", False),
        ("sys_platform != 'win32'", "win32", "CPython", False),
        ("sys_platform != 'win32'", "linux", "CPython", True),
        ("platform_python_implementation != 'PyPy'", "linux", "PyPy", False),
        ("platform_python_implementation != 'PyPy'", "linux", "CPython", True),
        ("implementation_name != 'PyPy'", "linux", "PyPy", False),
        ("implementation_name != 'PyPy'", "linux", "cpython", True),
    ],
)
def test_environment_check_evaluates_transitive_platform_markers(
    monkeypatch, marker, platform, implementation, required
):
    lock = _lock()
    lock["package"][0]["dependencies"][0]["marker"] = marker
    monkeypatch.setattr(checker, "sys", SimpleNamespace(implementation=SimpleNamespace(name=implementation)))
    monkeypatch.setattr(python_platform, "python_implementation", lambda: implementation)
    installed = {"demo": "1.2.3"}
    if platform != "win32":
        installed["uvloop"] = "0.22.1"

    assert verify_environment(lock, installed, platform) == (
        ["helper: locked dependency is not installed"] if required else []
    )


def test_environment_check_fails_closed_for_unknown_transitive_marker() -> None:
    lock = _lock()
    lock["package"][0]["dependencies"][0]["marker"] = "python_version >= '3.14'"

    assert verify_environment(lock, {"demo": "1.2.3"}, "win32") == [
        "helper: unsupported lock marker for container verification: python_version >= '3.14'"
    ]


def test_environment_check_requires_edge_to_resolve_to_locked_package() -> None:
    lock = _lock()
    lock["package"][0]["dependencies"] = [{"name": "not-locked"}]

    assert verify_environment(lock, {"demo": "1.2.3"}, "win32") == [
        "not-locked: dependency edge resolves to 0 locked packages"
    ]


def test_environment_check_selects_explicit_edge_version_and_rejects_other_locked_version() -> None:
    lock = _lock()
    lock["package"].append({"name": "helper", "version": "5.0", "source": {"registry": "https://pypi.org/simple"}})
    lock["package"][0]["dependencies"] = [
        {"name": "helper", "version": "4.5.6", "source": {"registry": "https://pypi.org/simple"}}
    ]

    assert verify_environment(lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32") == []
    assert verify_environment(lock, {"demo": "1.2.3", "helper": "5.0"}, "win32") == [
        "helper: installed 5.0, locked 4.5.6"
    ]


def test_environment_check_rejects_edge_source_mismatch() -> None:
    lock = _lock()
    lock["package"][0]["dependencies"][0]["source"] = {"registry": "https://unapproved.test/simple"}

    assert verify_environment(lock, {"demo": "1.2.3"}, "win32") == [
        "helper: dependency edge resolves to 0 locked packages"
    ]


def test_environment_check_fails_closed_for_ambiguous_edge() -> None:
    lock = _lock()
    lock["package"].append({"name": "helper", "version": "5.0", "source": {"registry": "https://pypi.org/simple"}})

    assert verify_environment(lock, {"demo": "1.2.3"}, "win32") == [
        "helper: dependency edge resolves to 2 locked packages"
    ]


def test_environment_check_rejects_conflicting_versions_selected_by_different_edges() -> None:
    lock = _lock()
    lock["package"].append({"name": "helper", "version": "5.0", "source": {"registry": "https://pypi.org/simple"}})
    lock["package"][0]["dependencies"][0]["version"] = "4.5.6"
    lock["package"][2]["dependencies"].append({"name": "helper", "version": "5.0"})

    assert "helper: dependency graph selects conflicting locked packages" in verify_environment(
        lock, {"demo": "1.2.3", "helper": "4.5.6"}, "win32"
    )


def test_environment_checker_cli_selects_dev_graph_and_returns_failing_exit_code(tmp_path) -> None:
    # No application imports or third-party packages are available in this child.
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(
        '[[package]]\nname = "sample-app"\nversion = "0"\nsource = { virtual = "." }\n'
        '[package.dev-dependencies]\ndev = [{ name = "missing-tool" }]\n'
        '[[package]]\nname = "missing-tool"\nversion = "1"\nsource = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    command = [sys.executable, "-S", "scripts/dependency_environment_check.py", "--lock", str(lock_path)]

    production = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    development = subprocess.run(
        command + ["--include-dev"], capture_output=True, text=True, encoding="utf-8", check=False
    )

    assert production.returncode == 0
    assert json.loads(production.stdout) == {"status": "passed", "errors": []}
    assert development.returncode == 1
    assert json.loads(development.stdout) == {
        "status": "failed",
        "errors": ["missing-tool: direct development dependency is not installed"],
    }


def test_lock_source_check_allows_only_pypi_registry_and_virtual_root() -> None:
    assert verify_lock_sources(_lock()) == []


def test_lock_source_check_rejects_direct_git_url_path_and_unapproved_registry() -> None:
    lock = _lock()
    lock["package"].extend(
        [
            {"name": "direct-url", "version": "1", "source": {"url": "https://example.test/pkg.whl"}},
            {"name": "git-package", "version": "1", "source": {"git": "https://example.test/repo"}},
            {"name": "local-path", "version": "1", "source": {"editable": "../outside"}},
            {
                "name": "alternate-index",
                "version": "1",
                "source": {"registry": "https://packages.example.test/simple"},
            },
        ]
    )

    errors = verify_lock_sources(lock)

    assert "alternate-index: unapproved registry https://packages.example.test/simple" in errors
    assert "direct-url: lock source must be the approved registry, found url" in errors
    assert "git-package: lock source must be the approved registry, found git" in errors
    assert "local-path: lock source must be the approved registry, found editable" in errors
