"""Tests for exact installed-versus-lock verification used by container CI."""

from scripts.dependency_environment_check import verify_environment


def _lock() -> dict[str, object]:
    return {
        "package": [
            {"name": "demo", "version": "1.2.3", "source": {"registry": "https://pypi.org/simple"}},
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
