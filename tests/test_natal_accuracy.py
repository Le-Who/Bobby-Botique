from pathlib import Path

import pytest

from app.natal.accuracy import (
    GOLDEN_CASES,
    NatalAccuracyResult,
    export_golden_cases_template,
    format_accuracy_results,
    load_golden_cases_from_json,
    validate_golden_cases,
)
from app.natal.horizons_accuracy import (
    HorizonsAccuracyResult,
    fetch_horizons_ecliptic_longitude,
    fetch_horizons_ecliptic_motion,
    format_horizons_results,
    validate_planets_against_horizons,
)
from scripts import natal_accuracy

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_validate_golden_cases_checks_core_chart_outputs():
    results = await validate_golden_cases()

    assert len(results) >= 2
    assert all(result.passed for result in results)
    assert all(result.checked_points >= 20 for result in results)
    assert {result.case_id for result in results} >= {"kyiv-1995-exact", "reading-1989-exact"}


@pytest.mark.asyncio
async def test_format_accuracy_results_marks_internal_references():
    results = await validate_golden_cases()

    output = format_accuracy_results(results)

    assert "PASS kyiv-1995-exact" in output
    assert "not externally verified" in output


@pytest.mark.asyncio
async def test_accuracy_cli_require_external_fails_for_internal_references(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "references.json"
    loaded_cases = (GOLDEN_CASES[0],)

    def fake_load_golden_cases_from_json(path):
        assert path == fixture_path
        return loaded_cases

    async def fake_validate_golden_cases(cases):
        assert cases == loaded_cases
        return [
            NatalAccuracyResult(
                case_id="internal-only",
                passed=True,
                checked_points=26,
                reference_source="internal-regression",
                externally_verified=False,
            )
        ]

    monkeypatch.setattr("scripts.natal_accuracy.load_golden_cases_from_json", fake_load_golden_cases_from_json)
    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_accuracy._main(require_external=True, fixture_path=fixture_path)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "not externally verified" in output
    assert "External accuracy verification is required" in output


@pytest.mark.asyncio
async def test_accuracy_cli_require_external_requires_reference_fixture(monkeypatch, capsys):
    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="external-looking",
                passed=True,
                checked_points=26,
                reference_source="external source",
                externally_verified=True,
            )
        ]

    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_accuracy._main(require_external=True)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "--reference-fixtures" in output


def test_load_golden_cases_from_json_marks_external_angle_references(tmp_path):
    fixture_path = tmp_path / "natal_angle_references.json"
    fixture_path.write_text(
        """
        {
          "cases": [
            {
              "case_id": "kyiv-1995-exact",
              "resolved": {
                "birth_input": {
                  "birth_date": "1995-02-14",
                  "time_precision": "exact",
                  "birth_time": "06:30",
                  "birth_place": "Kyiv, Ukraine"
                },
                "latitude": 50.4501,
                "longitude": 30.5234,
                "timezone": "Europe/Kyiv",
                "local_datetime": "1995-02-14T06:30:00+02:00",
                "utc_datetime": "1995-02-14T04:30:00+00:00",
                "display_place": "Kyiv, Ukraine"
              },
              "expected_planet_longitudes": {
                "sun": 325.0797,
                "moon": 129.1136,
                "mercury": 305.9419,
                "venus": 280.6519,
                "mars": 142.1335,
                "jupiter": 252.2866,
                "saturn": 342.6651,
                "uranus": 298.0993,
                "neptune": 294.2672,
                "pluto": 240.5742
              },
              "expected_retrogrades": {
                "sun": false,
                "moon": false,
                "mercury": true,
                "venus": false,
                "mars": true,
                "jupiter": false,
                "saturn": false,
                "uranus": false,
                "neptune": false,
                "pluto": false
              },
              "expected_angles": {
                "ascendant": 304.6017,
                "mc": 63.76
              },
              "expected_house_cusps": {
                "1": 304.6017,
                "2": 334.6017,
                "3": 4.6017,
                "4": 34.6017,
                "5": 64.6017,
                "6": 94.6017,
                "7": 124.6017,
                "8": 154.6017,
                "9": 184.6017,
                "10": 214.6017,
                "11": 244.6017,
                "12": 274.6017
              },
              "reference_source": "Astro-Seek manual check, 2026-06-08",
              "externally_verified": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    cases = load_golden_cases_from_json(fixture_path)

    assert len(cases) == 1
    assert cases[0].case_id == "kyiv-1995-exact"
    assert cases[0].externally_verified is True
    assert cases[0].expected_house_cusps[1] == pytest.approx(304.6017)
    assert "Astro-Seek" in cases[0].reference_source


def test_reference_fixture_example_is_structurally_valid_but_not_release_verified():
    cases = load_golden_cases_from_json(PROJECT_ROOT / "docs" / "natal-reference-fixture.example.json")

    assert cases
    assert all(not case.externally_verified for case in cases)
    assert all({"ascendant", "mc"} <= case.expected_angles.keys() for case in cases)
    assert all(set(range(1, 13)) <= case.expected_house_cusps.keys() for case in cases)


def test_moira_jpl_reference_fixture_satisfies_release_contract():
    cases = load_golden_cases_from_json(PROJECT_ROOT / "docs" / "natal-reference-fixture.moira-jpl.json")

    assert {case.case_id for case in cases} == {"kyiv-1995-exact", "reading-1989-exact"}
    assert all(case.externally_verified for case in cases)
    assert all("moira-astro 3.2.3" in case.reference_source for case in cases)
    assert all("NASA/JPL Horizons" in case.reference_source for case in cases)
    assert all({"ascendant", "mc"} <= case.expected_angles.keys() for case in cases)
    assert all(set(range(1, 13)) <= case.expected_house_cusps.keys() for case in cases)


def test_export_golden_cases_template_writes_current_fixture_shape(tmp_path):
    fixture_path = tmp_path / "exported-angle-references.json"

    export_golden_cases_template(fixture_path)
    cases = load_golden_cases_from_json(fixture_path)

    assert {case.case_id for case in cases} == {case.case_id for case in GOLDEN_CASES}
    assert all(not case.externally_verified for case in cases)
    assert all(set(range(1, 13)) <= case.expected_house_cusps.keys() for case in cases)
    assert "Replace with independently verified source" in fixture_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_accuracy_cli_exports_template_without_running_validation(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "angle-template.json"

    async def fake_validate_golden_cases():
        raise AssertionError("validation must not run for --export-template")

    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_accuracy._main(export_template_path=fixture_path)

    assert exit_code == 0
    assert fixture_path.exists()
    assert "exported" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_reference_fixture_example_does_not_satisfy_release_gate():
    fixture_path = PROJECT_ROOT / "docs" / "natal-reference-fixture.example.json"

    exit_code = await natal_accuracy._main(require_external=True, fixture_path=fixture_path)

    assert exit_code == 1


def test_load_golden_cases_from_json_rejects_external_case_without_all_planets(tmp_path):
    fixture_path = tmp_path / "missing_planets.json"
    fixture_path.write_text(
        """
        {
          "cases": [
            {
              "case_id": "partial-planets",
              "resolved": {
                "birth_input": {
                  "birth_date": "1995-02-14",
                  "time_precision": "exact",
                  "birth_time": "06:30",
                  "birth_place": "Kyiv, Ukraine"
                },
                "latitude": 50.4501,
                "longitude": 30.5234,
                "timezone": "Europe/Kyiv",
                "local_datetime": "1995-02-14T06:30:00+02:00",
                "utc_datetime": "1995-02-14T04:30:00+00:00",
                "display_place": "Kyiv, Ukraine"
              },
              "expected_planet_longitudes": {"sun": 325.0797},
              "expected_retrogrades": {
                "sun": false,
                "moon": false,
                "mercury": true,
                "venus": false,
                "mars": true,
                "jupiter": false,
                "saturn": false,
                "uranus": false,
                "neptune": false,
                "pluto": false
              },
              "expected_angles": {
                "ascendant": 304.6017,
                "mc": 63.76
              },
              "expected_house_cusps": {
                "1": 304.6017,
                "2": 334.6017,
                "3": 4.6017,
                "4": 34.6017,
                "5": 64.6017,
                "6": 94.6017,
                "7": 124.6017,
                "8": 154.6017,
                "9": 184.6017,
                "10": 214.6017,
                "11": 244.6017,
                "12": 274.6017
              },
              "reference_source": "external fixture",
              "externally_verified": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="all 10 planet longitudes"):
        load_golden_cases_from_json(fixture_path)


def test_load_golden_cases_from_json_rejects_external_case_without_all_retrograde_flags(tmp_path):
    fixture_path = tmp_path / "missing_retrogrades.json"
    fixture_path.write_text(
        """
        {
          "cases": [
            {
              "case_id": "partial-retrogrades",
              "resolved": {
                "birth_input": {
                  "birth_date": "1995-02-14",
                  "time_precision": "exact",
                  "birth_time": "06:30",
                  "birth_place": "Kyiv, Ukraine"
                },
                "latitude": 50.4501,
                "longitude": 30.5234,
                "timezone": "Europe/Kyiv",
                "local_datetime": "1995-02-14T06:30:00+02:00",
                "utc_datetime": "1995-02-14T04:30:00+00:00",
                "display_place": "Kyiv, Ukraine"
              },
              "expected_planet_longitudes": {
                "sun": 325.0797,
                "moon": 129.1136,
                "mercury": 305.9419,
                "venus": 280.6519,
                "mars": 142.1335,
                "jupiter": 252.2866,
                "saturn": 342.6651,
                "uranus": 298.0993,
                "neptune": 294.2672,
                "pluto": 240.5742
              },
              "expected_retrogrades": {"sun": false},
              "expected_angles": {
                "ascendant": 304.6017,
                "mc": 63.76
              },
              "expected_house_cusps": {
                "1": 304.6017,
                "2": 334.6017,
                "3": 4.6017,
                "4": 34.6017,
                "5": 64.6017,
                "6": 94.6017,
                "7": 124.6017,
                "8": 154.6017,
                "9": 184.6017,
                "10": 214.6017,
                "11": 244.6017,
                "12": 274.6017
              },
              "reference_source": "external fixture",
              "externally_verified": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="all 10 retrograde flags"):
        load_golden_cases_from_json(fixture_path)


def test_load_golden_cases_from_json_rejects_external_case_without_angles(tmp_path):
    fixture_path = tmp_path / "missing_angles.json"
    fixture_path.write_text(
        """
        {
          "cases": [
            {
              "case_id": "planet-only",
              "resolved": {
                "birth_input": {
                  "birth_date": "1995-02-14",
                  "time_precision": "exact",
                  "birth_time": "06:30",
                  "birth_place": "Kyiv, Ukraine"
                },
                "latitude": 50.4501,
                "longitude": 30.5234,
                "timezone": "Europe/Kyiv",
                "local_datetime": "1995-02-14T06:30:00+02:00",
                "utc_datetime": "1995-02-14T04:30:00+00:00",
                "display_place": "Kyiv, Ukraine"
              },
              "expected_planet_longitudes": {
                "sun": 325.0797,
                "moon": 129.1136,
                "mercury": 305.9419,
                "venus": 280.6519,
                "mars": 142.1335,
                "jupiter": 252.2866,
                "saturn": 342.6651,
                "uranus": 298.0993,
                "neptune": 294.2672,
                "pluto": 240.5742
              },
              "expected_retrogrades": {
                "sun": false,
                "moon": false,
                "mercury": true,
                "venus": false,
                "mars": true,
                "jupiter": false,
                "saturn": false,
                "uranus": false,
                "neptune": false,
                "pluto": false
              },
              "expected_angles": {"ascendant": 304.6017},
              "expected_house_cusps": {
                "1": 304.6017,
                "2": 334.6017,
                "3": 4.6017,
                "4": 34.6017,
                "5": 64.6017,
                "6": 94.6017,
                "7": 124.6017,
                "8": 154.6017,
                "9": 184.6017,
                "10": 214.6017,
                "11": 244.6017,
                "12": 274.6017
              },
              "reference_source": "external fixture",
              "externally_verified": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ascendant and mc"):
        load_golden_cases_from_json(fixture_path)


def test_load_golden_cases_from_json_rejects_external_case_without_all_house_cusps(tmp_path):
    fixture_path = tmp_path / "missing_house_cusps.json"
    fixture_path.write_text(
        """
        {
          "cases": [
            {
              "case_id": "partial-houses",
              "resolved": {
                "birth_input": {
                  "birth_date": "1995-02-14",
                  "time_precision": "exact",
                  "birth_time": "06:30",
                  "birth_place": "Kyiv, Ukraine"
                },
                "latitude": 50.4501,
                "longitude": 30.5234,
                "timezone": "Europe/Kyiv",
                "local_datetime": "1995-02-14T06:30:00+02:00",
                "utc_datetime": "1995-02-14T04:30:00+00:00",
                "display_place": "Kyiv, Ukraine"
              },
              "expected_planet_longitudes": {
                "sun": 325.0797,
                "moon": 129.1136,
                "mercury": 305.9419,
                "venus": 280.6519,
                "mars": 142.1335,
                "jupiter": 252.2866,
                "saturn": 342.6651,
                "uranus": 298.0993,
                "neptune": 294.2672,
                "pluto": 240.5742
              },
              "expected_retrogrades": {
                "sun": false,
                "moon": false,
                "mercury": true,
                "venus": false,
                "mars": true,
                "jupiter": false,
                "saturn": false,
                "uranus": false,
                "neptune": false,
                "pluto": false
              },
              "expected_angles": {
                "ascendant": 304.6017,
                "mc": 63.76
              },
              "expected_house_cusps": {
                "1": 304.6017,
                "2": 334.6017
              },
              "reference_source": "external fixture",
              "externally_verified": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="all 12 house cusps"):
        load_golden_cases_from_json(fixture_path)


def test_load_golden_cases_from_json_rejects_empty_case_list(tmp_path):
    fixture_path = tmp_path / "empty_references.json"
    fixture_path.write_text('{"cases": []}', encoding="utf-8")

    with pytest.raises(ValueError, match="at least one"):
        load_golden_cases_from_json(fixture_path)


@pytest.mark.asyncio
async def test_accuracy_cli_uses_external_fixture_cases(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "references.json"
    fixture_path.write_text('{"cases": []}', encoding="utf-8")
    loaded_cases = (GOLDEN_CASES[0],)
    calls = []

    def fake_load_golden_cases_from_json(path):
        calls.append(("load", path))
        return loaded_cases

    async def fake_validate_golden_cases(cases=GOLDEN_CASES):
        calls.append(("validate", cases))
        return [
            NatalAccuracyResult(
                case_id="external-case",
                passed=True,
                checked_points=26,
                reference_source="external fixture",
                externally_verified=True,
            )
        ]

    monkeypatch.setattr("scripts.natal_accuracy.load_golden_cases_from_json", fake_load_golden_cases_from_json)
    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_accuracy._main(require_external=True, fixture_path=fixture_path)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [("load", fixture_path), ("validate", loaded_cases)]
    assert "PASS external-case" in output


@pytest.mark.asyncio
async def test_accuracy_cli_require_external_fails_when_no_checks_ran(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "references.json"
    loaded_cases = (GOLDEN_CASES[0],)

    def fake_load_golden_cases_from_json(path):
        assert path == fixture_path
        return loaded_cases

    async def fake_validate_golden_cases(cases):
        assert cases == loaded_cases
        return []

    monkeypatch.setattr("scripts.natal_accuracy.load_golden_cases_from_json", fake_load_golden_cases_from_json)
    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_accuracy._main(require_external=True, fixture_path=fixture_path)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "No natal accuracy cases were checked" in output


@pytest.mark.asyncio
async def test_accuracy_cli_allows_internal_references_without_release_gate(monkeypatch):
    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="internal-only",
                passed=True,
                checked_points=26,
                reference_source="internal-regression",
                externally_verified=False,
            )
        ]

    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)

    assert await natal_accuracy._main(require_external=False) == 0


@pytest.mark.asyncio
async def test_fetch_horizons_ecliptic_longitude_parses_observer_table():
    class FakeResponse:
        text = """
$$SOE
 1995-Feb-14 04:30, , , 325.0092727, -0.0000734,
$$EOE
"""

        def raise_for_status(self):
            return None

    class FakeClient:
        async def get(self, url, params, timeout):
            assert "horizons.api" in url
            assert params["COMMAND"] == "10"
            assert params["QUANTITIES"] == "31"
            return FakeResponse()

    longitude = await fetch_horizons_ecliptic_longitude("sun", "1995-02-14T04:30:00+00:00", client=FakeClient())

    assert longitude == pytest.approx(325.0092727)


@pytest.mark.asyncio
async def test_fetch_horizons_ecliptic_motion_parses_first_and_last_rows():
    class FakeResponse:
        text = """
$$SOE
 1995-Feb-13 16:30, , , 306.1200000, -1.0000000,
 1995-Feb-14 04:30, , , 305.8700000, -1.0000000,
 1995-Feb-14 16:30, , , 305.6200000, -1.0000000,
$$EOE
"""

        def raise_for_status(self):
            return None

    class FakeClient:
        async def get(self, url, params, timeout):
            assert "horizons.api" in url
            assert params["COMMAND"] == "199"
            assert params["STEP_SIZE"] == "'12 h'"
            return FakeResponse()

    before, at_time, after = await fetch_horizons_ecliptic_motion(
        "mercury",
        "1995-02-14T04:30:00+00:00",
        client=FakeClient(),
    )

    assert before == pytest.approx(306.12)
    assert at_time == pytest.approx(305.87)
    assert after == pytest.approx(305.62)


@pytest.mark.asyncio
async def test_validate_planets_against_horizons_reports_external_planet_delta(monkeypatch):
    async def fake_fetch(planet_key, utc_datetime, *, client=None):
        del utc_datetime, client
        return {"sun": 325.01, "moon": 129.05}[planet_key]

    async def fake_motion(planet_key, utc_datetime, *, client=None):
        del utc_datetime, client
        return {
            "sun": (324.5, 325.01, 325.5),
            "moon": (128.5, 129.05, 129.5),
        }[planet_key]

    monkeypatch.setattr("app.natal.horizons_accuracy.fetch_horizons_ecliptic_longitude", fake_fetch)
    monkeypatch.setattr("app.natal.horizons_accuracy.fetch_horizons_ecliptic_motion", fake_motion)

    results = await validate_planets_against_horizons(
        cases=(GOLDEN_CASES[0],),
        planet_keys=("sun", "moon"),
        tolerance_degrees=0.2,
    )

    assert results
    assert all(result.passed for result in results)
    assert results[0].checked_points == 4
    assert all(result.externally_verified for result in results)
    assert "JPL Horizons" in format_horizons_results(results)


@pytest.mark.asyncio
async def test_accuracy_cli_can_run_horizons_external_check(monkeypatch, capsys):
    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=False,
            )
        ]

    async def fake_validate_planets_against_horizons():
        return [
            HorizonsAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=4,
                max_delta_degrees=0.1,
                externally_verified=True,
            )
        ]

    monkeypatch.setattr("scripts.natal_accuracy.validate_golden_cases", fake_validate_golden_cases)
    monkeypatch.setattr(
        "scripts.natal_accuracy.validate_planets_against_horizons",
        fake_validate_planets_against_horizons,
    )

    exit_code = await natal_accuracy._main(require_external=False, check_horizons=True)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS case-1: 4 JPL Horizons planet checks" in output
