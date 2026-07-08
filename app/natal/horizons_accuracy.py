from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from app.natal.accuracy import GOLDEN_CASES, NatalGoldenCase
from app.natal.astronomy import angular_distance
from app.natal.calculator import calculate_chart

HORIZONS_API_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
HORIZONS_LONGITUDE_TOLERANCE_DEGREES = 0.2

_PLANET_COMMANDS = {
    "sun": "10",
    "moon": "301",
    "mercury": "199",
    "venus": "299",
    "mars": "499",
    "jupiter": "599",
    "saturn": "699",
    "uranus": "799",
    "neptune": "899",
    "pluto": "999",
}

_HORIZONS_ROW_RE = re.compile(
    r"^\s*[^,\n]+,[^,\n]*,[^,\n]*,\s*([-+]?\d+(?:\.\d+)?)",
    re.MULTILINE,
)


class HorizonsClient(Protocol):
    async def get(self, url: str, *, params: dict[str, str], timeout: float) -> httpx.Response:
        ...


@dataclass(frozen=True)
class HorizonsAccuracyResult:
    case_id: str
    passed: bool
    checked_points: int
    max_delta_degrees: float
    failures: list[str] = field(default_factory=list)
    externally_verified: bool = True


async def fetch_horizons_ecliptic_longitude(
    planet_key: str,
    utc_datetime: str,
    *,
    client: HorizonsClient | None = None,
) -> float:
    command = _PLANET_COMMANDS[planet_key]
    start_dt = datetime.fromisoformat(utc_datetime.replace("Z", "+00:00"))
    stop_dt = start_dt + timedelta(minutes=1)
    params = {
        "format": "text",
        "COMMAND": command,
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": "500@399",
        "START_TIME": f"'{start_dt:%Y-%b-%d %H:%M}'",
        "STOP_TIME": f"'{stop_dt:%Y-%b-%d %H:%M}'",
        "STEP_SIZE": "'1 m'",
        "QUANTITIES": "31",
        "CSV_FORMAT": "YES",
        "ANG_FORMAT": "DEG",
        "APPARENT": "AIRLESS",
    }
    if client is not None:
        response = await client.get(HORIZONS_API_URL, params=params, timeout=20.0)
        response.raise_for_status()
        return _parse_horizons_longitude(response.text)
    async with httpx.AsyncClient() as owned_client:
        response = await owned_client.get(HORIZONS_API_URL, params=params, timeout=20.0)
        response.raise_for_status()
        return _parse_horizons_longitude(response.text)


async def fetch_horizons_ecliptic_motion(
    planet_key: str,
    utc_datetime: str,
    *,
    client: HorizonsClient | None = None,
) -> tuple[float, float, float]:
    command = _PLANET_COMMANDS[planet_key]
    center_dt = datetime.fromisoformat(utc_datetime.replace("Z", "+00:00"))
    start_dt = center_dt - timedelta(hours=12)
    stop_dt = center_dt + timedelta(hours=12)
    params = {
        "format": "text",
        "COMMAND": command,
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": "500@399",
        "START_TIME": f"'{start_dt:%Y-%b-%d %H:%M}'",
        "STOP_TIME": f"'{stop_dt:%Y-%b-%d %H:%M}'",
        "STEP_SIZE": "'12 h'",
        "QUANTITIES": "31",
        "CSV_FORMAT": "YES",
        "ANG_FORMAT": "DEG",
        "APPARENT": "AIRLESS",
    }
    if client is not None:
        response = await client.get(HORIZONS_API_URL, params=params, timeout=20.0)
        response.raise_for_status()
        return _parse_horizons_motion(response.text)
    async with httpx.AsyncClient() as owned_client:
        response = await owned_client.get(HORIZONS_API_URL, params=params, timeout=20.0)
        response.raise_for_status()
        return _parse_horizons_motion(response.text)


async def validate_planets_against_horizons(
    cases: tuple[NatalGoldenCase, ...] = GOLDEN_CASES,
    *,
    planet_keys: tuple[str, ...] = tuple(_PLANET_COMMANDS),
    tolerance_degrees: float = HORIZONS_LONGITUDE_TOLERANCE_DEGREES,
    client: HorizonsClient | None = None,
) -> list[HorizonsAccuracyResult]:
    results: list[HorizonsAccuracyResult] = []
    for case in cases:
        chart = await calculate_chart(case.resolved)
        planets = {planet.key: planet for planet in chart.planets}
        failures: list[str] = []
        max_delta = 0.0
        for planet_key in planet_keys:
            expected = await fetch_horizons_ecliptic_longitude(
                planet_key,
                case.resolved.utc_datetime,
                client=client,
            )
            actual = planets[planet_key].longitude
            delta = angular_distance(actual, expected)
            max_delta = max(max_delta, delta)
            if delta > tolerance_degrees:
                failures.append(
                    f"{planet_key} longitude differs from JPL Horizons by {delta:.4f} deg "
                    f"(local={actual:.4f}, horizons={expected:.4f})"
                )
            before, _at_time, after = await fetch_horizons_ecliptic_motion(
                planet_key,
                case.resolved.utc_datetime,
                client=client,
            )
            horizons_retrograde = _motion_is_retrograde(before, after)
            if planets[planet_key].retrograde is not horizons_retrograde:
                failures.append(
                    f"{planet_key} retrograde differs from JPL Horizons "
                    f"(local={planets[planet_key].retrograde}, horizons={horizons_retrograde})"
                )
        results.append(
            HorizonsAccuracyResult(
                case_id=case.case_id,
                passed=not failures,
                checked_points=len(planet_keys) * 2,
                max_delta_degrees=max_delta,
                failures=failures,
            )
        )
    return results


def format_horizons_results(results: list[HorizonsAccuracyResult]) -> str:
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(
            f"{status} {result.case_id}: {result.checked_points} JPL Horizons planet checks, "
            f"max_delta={result.max_delta_degrees:.4f} deg"
        )
        for failure in result.failures:
            lines.append(f"  - {failure}")
    return "\n".join(lines)


def _parse_horizons_longitude(text: str) -> float:
    values = _parse_horizons_longitudes(text)
    if not values:
        raise ValueError("Could not parse JPL Horizons ObsEcLon from response.")
    return values[0]


def _parse_horizons_motion(text: str) -> tuple[float, float, float]:
    values = _parse_horizons_longitudes(text)
    if len(values) < 3:
        raise ValueError("Could not parse JPL Horizons motion rows from response.")
    return values[0], values[len(values) // 2], values[-1]


def _parse_horizons_longitudes(text: str) -> list[float]:
    try:
        start = text.index("$$SOE")
        end = text.index("$$EOE", start)
    except ValueError:
        return []
    table = text[start:end]
    return [float(match.group(1)) for match in _HORIZONS_ROW_RE.finditer(table)]


def _motion_is_retrograde(before: float, after: float) -> bool:
    movement = (after - before + 180.0) % 360.0 - 180.0
    return movement < 0.0
