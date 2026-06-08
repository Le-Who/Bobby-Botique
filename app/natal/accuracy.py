from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.natal.astronomy import angular_distance
from app.natal.calculator import calculate_chart
from app.natal.models import BirthInput, ResolvedBirthData, TimePrecision

LONGITUDE_TOLERANCE_DEGREES = 0.05
ANGLE_TOLERANCE_DEGREES = 0.1
REQUIRED_PLANET_KEYS = frozenset(
    {
        "sun",
        "moon",
        "mercury",
        "venus",
        "mars",
        "jupiter",
        "saturn",
        "uranus",
        "neptune",
        "pluto",
    }
)


@dataclass(frozen=True)
class NatalGoldenCase:
    case_id: str
    resolved: ResolvedBirthData
    expected_planet_longitudes: dict[str, float]
    expected_retrogrades: dict[str, bool]
    expected_angles: dict[str, float]
    expected_house_cusps: dict[int, float]
    reference_source: str
    externally_verified: bool = False


@dataclass(frozen=True)
class NatalAccuracyResult:
    case_id: str
    passed: bool
    checked_points: int
    failures: list[str] = field(default_factory=list)
    reference_source: str = ""
    externally_verified: bool = False


GOLDEN_CASES: tuple[NatalGoldenCase, ...] = (
    NatalGoldenCase(
        case_id="kyiv-1995-exact",
        resolved=ResolvedBirthData(
            birth_input=BirthInput(
                birth_date="1995-02-14",
                time_precision=TimePrecision.EXACT,
                birth_time="06:30",
                birth_place="Kyiv, Ukraine",
            ),
            latitude=50.4501,
            longitude=30.5234,
            timezone="Europe/Kyiv",
            local_datetime="1995-02-14T06:30:00+02:00",
            utc_datetime="1995-02-14T04:30:00+00:00",
            display_place="Kyiv, Ukraine",
        ),
        expected_planet_longitudes={
            "sun": 325.0797,
            "moon": 129.1136,
            "mercury": 305.9419,
            "venus": 280.6519,
            "mars": 142.1335,
            "jupiter": 252.2866,
            "saturn": 342.6651,
            "uranus": 298.0993,
            "neptune": 294.2672,
            "pluto": 240.5742,
        },
        expected_retrogrades={
            "sun": False,
            "moon": False,
            "mercury": True,
            "venus": False,
            "mars": True,
            "jupiter": False,
            "saturn": False,
            "uranus": False,
            "neptune": False,
            "pluto": False,
        },
        expected_angles={"ascendant": 304.6123, "mc": 243.7638},
        expected_house_cusps={1: 304.6123, 2: 334.6123, 3: 4.6123, 4: 34.6123},
        reference_source="internal-regression; replace or mark externally_verified after independent Swiss/Astro-Seek check",
    ),
    NatalGoldenCase(
        case_id="reading-1989-exact",
        resolved=ResolvedBirthData(
            birth_input=BirthInput(
                birth_date="1989-12-13",
                time_precision=TimePrecision.EXACT,
                birth_time="05:30",
                birth_place="Reading, United States",
            ),
            latitude=40.3356,
            longitude=-75.9269,
            timezone="America/New_York",
            local_datetime="1989-12-13T05:30:00-05:00",
            utc_datetime="1989-12-13T10:30:00+00:00",
            display_place="Reading, Pennsylvania, United States",
        ),
        expected_planet_longitudes={
            "sun": 261.5416,
            "moon": 91.7661,
            "mercury": 278.7933,
            "venus": 301.9893,
            "mars": 236.8347,
            "jupiter": 97.8204,
            "saturn": 283.5875,
            "uranus": 274.7872,
            "neptune": 281.471,
            "pluto": 226.6386,
        },
        expected_retrogrades={
            "sun": False,
            "moon": False,
            "mercury": False,
            "venus": False,
            "mars": False,
            "jupiter": True,
            "saturn": False,
            "uranus": False,
            "neptune": False,
            "pluto": False,
        },
        expected_angles={"ascendant": 238.1609, "mc": 162.2794},
        expected_house_cusps={1: 238.1609, 2: 268.1609, 3: 298.1609, 4: 328.1609},
        reference_source="internal-regression; replace or mark externally_verified after independent Swiss/Astro-Seek check",
    ),
)


def load_golden_cases_from_json(path: str | Path) -> tuple[NatalGoldenCase, ...]:
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("Natal accuracy fixture must contain a 'cases' list.")
    if not raw_cases:
        raise ValueError("Natal accuracy fixture must contain at least one case.")

    cases: list[NatalGoldenCase] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("Each natal accuracy fixture case must be an object.")
        expected_house_cusps = {
            int(number): float(longitude)
            for number, longitude in raw_case.get("expected_house_cusps", {}).items()
        }
        expected_planet_longitudes = {
            str(key): float(value)
            for key, value in raw_case.get("expected_planet_longitudes", {}).items()
        }
        expected_retrogrades = {
            str(key): bool(value)
            for key, value in raw_case.get("expected_retrogrades", {}).items()
        }
        expected_angles = {str(key): float(value) for key, value in raw_case.get("expected_angles", {}).items()}
        externally_verified = bool(raw_case.get("externally_verified", False))
        if externally_verified:
            _validate_external_case_completeness(
                case_id=str(raw_case["case_id"]),
                expected_planet_longitudes=expected_planet_longitudes,
                expected_retrogrades=expected_retrogrades,
                expected_angles=expected_angles,
                expected_house_cusps=expected_house_cusps,
            )
        cases.append(
            NatalGoldenCase(
                case_id=str(raw_case["case_id"]),
                resolved=ResolvedBirthData.model_validate(raw_case["resolved"]),
                expected_planet_longitudes=expected_planet_longitudes,
                expected_retrogrades=expected_retrogrades,
                expected_angles=expected_angles,
                expected_house_cusps=expected_house_cusps,
                reference_source=str(raw_case["reference_source"]),
                externally_verified=externally_verified,
            )
        )
    return tuple(cases)


def export_golden_cases_template(
    path: str | Path,
    cases: tuple[NatalGoldenCase, ...] = GOLDEN_CASES,
) -> None:
    fixture_path = Path(path)
    payload = {"cases": [_golden_case_to_fixture(case) for case in cases]}
    fixture_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _golden_case_to_fixture(case: NatalGoldenCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "resolved": case.resolved.model_dump(mode="json"),
        "expected_planet_longitudes": case.expected_planet_longitudes,
        "expected_retrogrades": case.expected_retrogrades,
        "expected_angles": case.expected_angles,
        "expected_house_cusps": {
            str(number): longitude for number, longitude in sorted(_complete_equal_house_cusps(case).items())
        },
        "reference_source": (
            "Template only: internal regression values copied from app.natal.accuracy. "
            "Replace with independently verified source before setting externally_verified=true."
        ),
        "externally_verified": False,
    }


def _complete_equal_house_cusps(case: NatalGoldenCase) -> dict[int, float]:
    if 1 not in case.expected_house_cusps:
        return dict(case.expected_house_cusps)
    first_cusp = case.expected_house_cusps[1]
    return {number: (first_cusp + (number - 1) * 30.0) % 360.0 for number in range(1, 13)}


def _validate_external_case_completeness(
    *,
    case_id: str,
    expected_planet_longitudes: dict[str, float],
    expected_retrogrades: dict[str, bool],
    expected_angles: dict[str, float],
    expected_house_cusps: dict[int, float],
) -> None:
    if REQUIRED_PLANET_KEYS - expected_planet_longitudes.keys():
        raise ValueError(f"Externally verified natal case {case_id} must include all 10 planet longitudes.")
    if REQUIRED_PLANET_KEYS - expected_retrogrades.keys():
        raise ValueError(f"Externally verified natal case {case_id} must include all 10 retrograde flags.")
    if {"ascendant", "mc"} - expected_angles.keys():
        raise ValueError(f"Externally verified natal case {case_id} must include ascendant and mc.")
    expected_houses = set(range(1, 13))
    if expected_houses - expected_house_cusps.keys():
        raise ValueError(f"Externally verified natal case {case_id} must include all 12 house cusps.")


async def validate_golden_cases(
    cases: tuple[NatalGoldenCase, ...] = GOLDEN_CASES,
    *,
    longitude_tolerance: float = LONGITUDE_TOLERANCE_DEGREES,
    angle_tolerance: float = ANGLE_TOLERANCE_DEGREES,
) -> list[NatalAccuracyResult]:
    results: list[NatalAccuracyResult] = []
    for case in cases:
        chart = await calculate_chart(case.resolved)
        planets = {planet.key: planet for planet in chart.planets}
        houses = {house.number: house for house in chart.houses}
        failures: list[str] = []
        checked_points = 0

        for key, expected in case.expected_planet_longitudes.items():
            checked_points += 1
            actual = planets[key].longitude
            delta = angular_distance(actual, expected)
            if delta > longitude_tolerance:
                failures.append(f"{key} longitude expected {expected:.4f}, got {actual:.4f}, delta {delta:.4f}")

        for key, expected in case.expected_retrogrades.items():
            checked_points += 1
            actual = planets[key].retrograde
            if actual is not expected:
                failures.append(f"{key} retrograde expected {expected}, got {actual}")

        for key, expected in case.expected_angles.items():
            checked_points += 1
            actual = chart.angles[key]
            delta = angular_distance(actual, expected)
            if delta > angle_tolerance:
                failures.append(f"{key} expected {expected:.4f}, got {actual:.4f}, delta {delta:.4f}")

        for number, expected in case.expected_house_cusps.items():
            checked_points += 1
            actual = houses[number].cusp_longitude
            delta = angular_distance(actual, expected)
            if delta > angle_tolerance:
                failures.append(f"house {number} expected {expected:.4f}, got {actual:.4f}, delta {delta:.4f}")

        results.append(
            NatalAccuracyResult(
                case_id=case.case_id,
                passed=not failures,
                checked_points=checked_points,
                failures=failures,
                reference_source=case.reference_source,
                externally_verified=case.externally_verified,
            )
        )
    return results


def format_accuracy_results(results: list[NatalAccuracyResult]) -> str:
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        verification = "externally verified" if result.externally_verified else "not externally verified"
        lines.append(f"{status} {result.case_id}: {result.checked_points} checks, {verification}")
        if result.reference_source:
            lines.append(f"  source: {result.reference_source}")
        for failure in result.failures:
            lines.append(f"  - {failure}")
    return "\n".join(lines)
