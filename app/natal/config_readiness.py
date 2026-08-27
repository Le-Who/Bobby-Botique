from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from app.natal.city_catalog import load_city_overrides


@dataclass(frozen=True)
class NatalConfigReadinessResult:
    passed: bool
    status: str
    failures: list[str] = field(default_factory=list)


_REQUIRED_PACKAGES = {
    "geonamescache": ((3, 0, 1), (4, 0, 0)),
    "ephem": ((4, 1, 0), (5, 0, 0)),
    "tzdata": ((2024, 1), None),
}


def check_natal_config_readiness(
    settings: Any,
    *,
    webhook_url: str,
    package_versions: Mapping[str, str | None] | None = None,
    python_version: tuple[int, ...] | None = None,
) -> NatalConfigReadinessResult:
    failures: list[str] = []
    reports_enabled = bool(getattr(settings, "NATAL_REPORTS_ENABLED", False))
    geocoder_provider = str(getattr(settings, "NATAL_GEOCODER_PROVIDER", "local") or "local").strip().lower()
    ttl_days = int(getattr(settings, "NATAL_REPORT_TTL_DAYS", 0) or 0)
    sends_raw_birth_data = bool(getattr(settings, "NATAL_SEND_RAW_BIRTH_DATA_TO_LLM", False))
    web_server_enabled = bool(getattr(settings, "ENABLE_WEB_SERVER", False))
    city_overrides_path = str(getattr(settings, "NATAL_CITY_OVERRIDES_PATH", "") or "").strip()
    normalized_webhook_url = webhook_url.strip().rstrip("/")

    if not reports_enabled:
        failures.append("NATAL_REPORTS_ENABLED must be true for release readiness.")
    if ttl_days <= 0:
        failures.append("NATAL_REPORT_TTL_DAYS must be positive.")
    if geocoder_provider != "local":
        failures.append("NATAL_GEOCODER_PROVIDER must be local for release readiness.")
    if sends_raw_birth_data:
        failures.append("NATAL_SEND_RAW_BIRTH_DATA_TO_LLM must remain false.")
    if not web_server_enabled:
        failures.append("ENABLE_WEB_SERVER must be true to serve hosted natal reports.")
    if not normalized_webhook_url:
        failures.append("WEBHOOK_URL is required for hosted natal reports.")
    elif not normalized_webhook_url.startswith("https://"):
        failures.append("WEBHOOK_URL must use https:// for release readiness.")

    runtime_python_version = python_version or tuple(sys.version_info[:3])
    if runtime_python_version < (3, 14):
        failures.append("Python 3.14+ is required for the target natal release environment.")

    versions = package_versions if package_versions is not None else _installed_package_versions()
    for package_name, (minimum, maximum) in _REQUIRED_PACKAGES.items():
        version = versions.get(package_name)
        if not version:
            failures.append(_missing_package_message(package_name, minimum, maximum))
            continue
        parsed_version = _parse_version(version)
        if parsed_version is None or parsed_version < minimum or (maximum is not None and parsed_version >= maximum):
            failures.append(_missing_package_message(package_name, minimum, maximum))
    if city_overrides_path:
        try:
            load_city_overrides(city_overrides_path)
        except ValueError as exc:
            failures.append(f"NATAL_CITY_OVERRIDES_PATH is invalid: {exc}")

    return NatalConfigReadinessResult(
        passed=not failures,
        status="ready" if not failures else "not-ready",
        failures=failures,
    )


def format_natal_config_readiness(result: NatalConfigReadinessResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    lines = [f"{status} natal-config: {result.status}"]
    for failure in result.failures:
        lines.append(f"  - {failure}")
    return "\n".join(lines)


def _installed_package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package_name in _REQUIRED_PACKAGES:
        try:
            versions[package_name] = metadata.version(package_name)
        except metadata.PackageNotFoundError:
            versions[package_name] = None
    return versions


def _parse_version(value: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _missing_package_message(
    package_name: str,
    minimum: tuple[int, ...],
    maximum: tuple[int, ...] | None,
) -> str:
    if maximum is None:
        return f"{package_name} must be installed with version >={_format_version(minimum)}."
    return f"{package_name} must be installed with version >={_format_version(minimum)},<{_format_version(maximum)}."
