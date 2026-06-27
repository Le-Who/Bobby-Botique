from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from app.natal.city_catalog import search_cities, warm_city_catalog

RELEASE_CITY_CASES: tuple[tuple[str, str], ...] = (
    ("Odesa", "Europe/"),
    ("Kyiv", "Europe/"),
    ("Moscow", "Europe/"),
    ("London", "Europe/"),
    ("New York", "America/"),
    ("Ottawa", "America/"),
    ("Orenburg", "Asia/"),
    ("Berlin", "Europe/"),
    ("Warsaw", "Europe/"),
    ("Istanbul", "Europe/"),
)

RELEASE_AUTOCOMPLETE_CASES: tuple[tuple[str, str, str, str], ...] = (
    ("О", "Оде", "UA", "Odesa"),
    ("О", "Оре", "RU", "Orenburg"),
    ("О", "Отт", "CA", "Ottawa"),
)
AUTOCOMPLETE_READINESS_LIMIT = 30
MIN_CITY_CATALOG_SIZE = 30_000

RELEASE_DISAMBIGUATION_CASES: tuple[tuple[str, str, str], ...] = (
    ("Reading, Massachusetts", "US", "Reading, Massachusetts, United States"),
    ("Reading, Pennsylvania", "US", "Reading, Pennsylvania, United States"),
)


class CityLike(Protocol):
    name: str
    display_name: str
    latitude: float
    longitude: float
    timezone: str


@dataclass(frozen=True)
class CityCheck:
    query: str
    matched_display_name: str
    latitude: float
    longitude: float
    timezone: str
    search_ms: float


@dataclass(frozen=True)
class CityAutocompleteCheck:
    broad_query: str
    narrow_query: str
    country_code: str
    expected_name: str
    broad_count: int
    narrow_count: int
    search_ms: float


@dataclass(frozen=True)
class CityDisambiguationCheck:
    query: str
    country_code: str
    expected_display_name: str
    matched_display_name: str
    search_ms: float


@dataclass(frozen=True)
class CityReadinessResult:
    passed: bool
    city_count: int
    warmup_ms: float
    checked_cases: int
    checks: list[CityCheck] = field(default_factory=list)
    autocomplete_checks: list[CityAutocompleteCheck] = field(default_factory=list)
    disambiguation_checks: list[CityDisambiguationCheck] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def check_city_catalog_readiness(
    *,
    cases: Sequence[tuple[str, str]] = RELEASE_CITY_CASES,
    autocomplete_cases: Sequence[tuple[str, str, str, str]] = RELEASE_AUTOCOMPLETE_CASES,
    disambiguation_cases: Sequence[tuple[str, str, str]] = RELEASE_DISAMBIGUATION_CASES,
    search_cities_fn: Callable[[str, int, str | None], Any] = search_cities,
    warm_city_catalog_fn: Callable[[], int] = warm_city_catalog,
    max_warmup_ms: float | None = None,
    max_search_ms: float | None = None,
    min_city_count: int = MIN_CITY_CATALOG_SIZE,
) -> CityReadinessResult:
    warmup_start = perf_counter()
    city_count = warm_city_catalog_fn()
    warmup_ms = (perf_counter() - warmup_start) * 1000

    checks: list[CityCheck] = []
    failures: list[str] = []
    if city_count < min_city_count:
        failures.append(f"catalog city count below {min_city_count}: {city_count}")
    if max_warmup_ms is not None and warmup_ms > max_warmup_ms:
        failures.append(f"catalog warmup exceeded {max_warmup_ms:.1f} ms: {warmup_ms:.1f} ms")
    for query, timezone_prefix in cases:
        search_start = perf_counter()
        matches = list(search_cities_fn(query, 5, None))
        search_ms = (perf_counter() - search_start) * 1000
        if max_search_ms is not None and search_ms > max_search_ms:
            failures.append(f"{query}: search exceeded {max_search_ms:.1f} ms: {search_ms:.1f} ms")
        if not matches:
            failures.append(f"{query}: no local city match")
            continue
        city = matches[0]
        checks.append(
            CityCheck(
                query=query,
                matched_display_name=city.display_name,
                latitude=city.latitude,
                longitude=city.longitude,
                timezone=city.timezone,
                search_ms=search_ms,
            )
        )
        if not city.timezone.startswith(timezone_prefix):
            failures.append(f"{query}: expected timezone prefix {timezone_prefix}, got {city.timezone}")
        if not city.latitude or not city.longitude:
            failures.append(f"{query}: missing coordinates")

    autocomplete_checks: list[CityAutocompleteCheck] = []
    for broad_query, narrow_query, country_code, expected_name in autocomplete_cases:
        search_start = perf_counter()
        broad_matches = list(search_cities_fn(broad_query, AUTOCOMPLETE_READINESS_LIMIT, country_code))
        narrow_matches = list(search_cities_fn(narrow_query, AUTOCOMPLETE_READINESS_LIMIT, country_code))
        search_ms = (perf_counter() - search_start) * 1000
        if max_search_ms is not None and search_ms > max_search_ms:
            failures.append(
                f"{country_code} {broad_query}->{narrow_query}: autocomplete search exceeded "
                f"{max_search_ms:.1f} ms: {search_ms:.1f} ms"
            )
        broad_names = {city.name for city in broad_matches}
        narrow_names = {city.name for city in narrow_matches}
        autocomplete_checks.append(
            CityAutocompleteCheck(
                broad_query=broad_query,
                narrow_query=narrow_query,
                country_code=country_code,
                expected_name=expected_name,
                broad_count=len(broad_matches),
                narrow_count=len(narrow_matches),
                search_ms=search_ms,
            )
        )
        if expected_name not in broad_names:
            failures.append(f"{country_code} {broad_query}: expected {expected_name} in autocomplete results")
        if expected_name not in narrow_names:
            failures.append(f"{country_code} {narrow_query}: expected {expected_name} in autocomplete results")
        if len(narrow_matches) >= len(broad_matches):
            failures.append(
                f"{country_code} {broad_query}->{narrow_query}: expected narrowed results, "
                f"got broad={len(broad_matches)} narrow={len(narrow_matches)}"
            )

    disambiguation_checks: list[CityDisambiguationCheck] = []
    for query, country_code, expected_display_name in disambiguation_cases:
        search_start = perf_counter()
        matches = list(search_cities_fn(query, 5, country_code))
        search_ms = (perf_counter() - search_start) * 1000
        if max_search_ms is not None and search_ms > max_search_ms:
            failures.append(f"{country_code} {query}: disambiguation search exceeded {max_search_ms:.1f} ms: {search_ms:.1f} ms")
        if not matches:
            failures.append(f"{country_code} {query}: no local city match")
            continue
        matched_display_name = matches[0].display_name
        disambiguation_checks.append(
            CityDisambiguationCheck(
                query=query,
                country_code=country_code,
                expected_display_name=expected_display_name,
                matched_display_name=matched_display_name,
                search_ms=search_ms,
            )
        )
        if matched_display_name != expected_display_name:
            failures.append(f"{country_code} {query}: expected {expected_display_name}, got {matched_display_name}")

    return CityReadinessResult(
        passed=not failures,
        city_count=city_count,
        warmup_ms=warmup_ms,
        checked_cases=len(cases) + len(autocomplete_cases) + len(disambiguation_cases),
        checks=checks,
        autocomplete_checks=autocomplete_checks,
        disambiguation_checks=disambiguation_checks,
        failures=failures,
    )


def format_city_readiness(result: CityReadinessResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    max_search_ms = max((check.search_ms for check in result.checks), default=0.0)
    lines = [
        (
            f"{status} natal-city-catalog: cities={result.city_count} "
            f"checked={result.checked_cases} warmup_ms={result.warmup_ms:.1f} search_ms={max_search_ms:.1f}"
        )
    ]
    for check in result.checks:
        lines.append(
            f"  {check.query} -> {check.matched_display_name} "
            f"({check.latitude:.5f}, {check.longitude:.5f}, {check.timezone})"
        )
    for ac_check in result.autocomplete_checks:
        lines.append(
            f"  autocomplete {ac_check.country_code} {ac_check.broad_query}->{ac_check.narrow_query}: "
            f"{ac_check.expected_name} broad={ac_check.broad_count} narrow={ac_check.narrow_count}"
        )
    for d_check in result.disambiguation_checks:
        lines.append(f"  disambiguation {d_check.country_code} {d_check.query}: {d_check.matched_display_name}")
    for failure in result.failures:
        lines.append(f"  - {failure}")
    return "\n".join(lines)
