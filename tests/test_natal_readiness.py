import pytest

from app.natal.accuracy import NatalAccuracyResult
from app.natal.city_readiness import CityReadinessResult
from app.natal.config_readiness import NatalConfigReadinessResult
from app.natal.horizons_accuracy import HorizonsAccuracyResult
from app.natal.smoke import NatalSmokeResult
from scripts import natal_readiness


@pytest.mark.asyncio
async def test_readiness_cli_passes_local_city_and_accuracy_checks(monkeypatch, capsys):
    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=False,
            )
        ]

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS natal-city-catalog" in output
    assert "PASS case-1" in output


@pytest.mark.asyncio
async def test_readiness_cli_fails_release_gate_without_external_accuracy(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "references.json"
    loaded_cases = (object(),)

    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    def fake_load_golden_cases_from_json(path):
        assert path == fixture_path
        return loaded_cases

    async def fake_validate_golden_cases(cases):
        assert cases == loaded_cases
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=False,
            )
        ]

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.load_golden_cases_from_json", fake_load_golden_cases_from_json)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_readiness._main(
        require_external=True,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=False,
        fixture_path=fixture_path,
    )

    assert exit_code == 1
    assert "External accuracy verification is required" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_readiness_cli_require_external_requires_reference_fixture(monkeypatch, capsys):
    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="external-looking",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_readiness._main(
        require_external=True,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "--reference-fixtures" in output


@pytest.mark.asyncio
async def test_readiness_cli_fails_when_no_accuracy_cases_checked(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "references.json"
    loaded_cases = (object(),)

    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    def fake_load_golden_cases_from_json(path):
        assert path == fixture_path
        return loaded_cases

    async def fake_validate_golden_cases(cases):
        assert cases == loaded_cases
        return []

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.load_golden_cases_from_json", fake_load_golden_cases_from_json)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_readiness._main(
        require_external=True,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=False,
        fixture_path=fixture_path,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "No natal accuracy cases were checked" in output


@pytest.mark.asyncio
async def test_readiness_cli_uses_external_accuracy_fixture(monkeypatch, tmp_path, capsys):
    fixture_path = tmp_path / "angle-references.json"
    fixture_path.write_text('{"cases": []}', encoding="utf-8")
    loaded_cases = (object(),)
    calls = []

    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    def fake_load_golden_cases_from_json(path):
        calls.append(("load", path))
        return loaded_cases

    async def fake_validate_golden_cases(cases):
        calls.append(("accuracy", cases))
        return [
            NatalAccuracyResult(
                case_id="external-case",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.load_golden_cases_from_json", fake_load_golden_cases_from_json)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=False,
        fixture_path=fixture_path,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [("load", fixture_path), ("accuracy", loaded_cases)]
    assert "PASS external-case" in output


@pytest.mark.asyncio
async def test_readiness_cli_runs_storage_and_smoke_when_requested(monkeypatch, capsys):
    calls = []

    def fake_check_city_catalog_readiness(**kwargs):
        calls.append("city")
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        calls.append("accuracy")
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    async def fake_check_storage_ready():
        calls.append("storage")

    async def fake_run_natal_smoke(webhook_url: str, user_id: int, chat_id: int):
        calls.append(("smoke", webhook_url, user_id, chat_id))
        return NatalSmokeResult(
            report_id="report-1",
            hosted_url=f"{webhook_url}/reports/natal/report-1",
            telegraph_url=None,
            planet_count=10,
            section_count=4,
            hosted_html_contains_svg=True,
            hosted_html_contains_sections=True,
        )

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)
    monkeypatch.setattr("scripts.natal_readiness.check_storage_ready", fake_check_storage_ready)
    monkeypatch.setattr("scripts.natal_readiness.run_natal_smoke", fake_run_natal_smoke)

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=True,
        webhook_url="https://bot.example.com",
        user_id=1,
        chat_id=2,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=True,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == ["city", "accuracy", "storage", ("smoke", "https://bot.example.com", 1, 2)]
    assert "storage=ready" in output
    assert "smoke_report_id=report-1" in output


@pytest.mark.asyncio
async def test_readiness_cli_passes_city_latency_thresholds(monkeypatch):
    captured = {}

    def fake_check_city_catalog_readiness(*, max_warmup_ms=None, max_search_ms=None, min_city_count=30000):
        captured["max_warmup_ms"] = max_warmup_ms
        captured["max_search_ms"] = max_search_ms
        captured["min_city_count"] = min_city_count
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=1500.0,
        max_city_search_ms=100.0,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=False,
    )

    assert exit_code == 0
    assert captured == {"max_warmup_ms": 1500.0, "max_search_ms": 100.0, "min_city_count": 30000}


@pytest.mark.asyncio
async def test_readiness_cli_runs_optional_horizons_accuracy_check(monkeypatch, capsys):
    calls = []

    def fake_check_city_catalog_readiness(**kwargs):
        calls.append("city")
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        calls.append("accuracy")
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=False,
            )
        ]

    async def fake_validate_planets_against_horizons():
        calls.append("horizons")
        return [
            HorizonsAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=20,
                max_delta_degrees=0.1,
            )
        ]

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)
    monkeypatch.setattr(
        "scripts.natal_readiness.validate_planets_against_horizons",
        fake_validate_planets_against_horizons,
    )

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=True,
        check_config=False,
        run_smoke=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == ["city", "accuracy", "horizons"]
    assert "PASS case-1: 20 JPL Horizons planet checks" in output


@pytest.mark.asyncio
async def test_readiness_cli_runs_optional_config_check(monkeypatch, capsys):
    calls = []

    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    def fake_check_natal_config_readiness(settings, *, webhook_url):
        calls.append((settings, webhook_url))
        return NatalConfigReadinessResult(passed=True, status="ready")

    async def fake_run_natal_smoke(webhook_url: str, user_id: int, chat_id: int):
        return NatalSmokeResult(
            report_id="report-1",
            hosted_url=f"{webhook_url}/reports/natal/report-1",
            telegraph_url=None,
            planet_count=10,
            section_count=4,
            hosted_html_contains_svg=True,
            hosted_html_contains_sections=True,
        )

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)
    monkeypatch.setattr("scripts.natal_readiness.run_natal_smoke", fake_run_natal_smoke)
    monkeypatch.setattr(
        "scripts.natal_readiness.check_natal_config_readiness",
        fake_check_natal_config_readiness,
    )
    monkeypatch.setattr("scripts.natal_readiness.settings", object())

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="https://bot.example.com",
        user_id=0,
        chat_id=0,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=True,
        run_smoke=False,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [(natal_readiness.settings, "https://bot.example.com")]
    assert "PASS natal-config: ready" in output
    assert "smoke_report_id" not in output


@pytest.mark.asyncio
async def test_readiness_cli_requires_explicit_smoke_flag_for_live_report(monkeypatch, capsys):
    calls = []

    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    async def fake_run_natal_smoke(webhook_url: str, user_id: int, chat_id: int):
        calls.append(("smoke", webhook_url, user_id, chat_id))
        return NatalSmokeResult(
            report_id="report-1",
            hosted_url=f"{webhook_url}/reports/natal/report-1",
            telegraph_url=None,
            planet_count=10,
            section_count=4,
            hosted_html_contains_svg=True,
            hosted_html_contains_sections=True,
        )

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)
    monkeypatch.setattr("scripts.natal_readiness.run_natal_smoke", fake_run_natal_smoke)

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="https://bot.example.com",
        user_id=11,
        chat_id=22,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=False,
        run_smoke=True,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert calls == [("smoke", "https://bot.example.com", 11, 22)]
    assert "smoke_report_id=report-1" in output


@pytest.mark.asyncio
async def test_readiness_cli_does_not_run_smoke_when_config_check_fails(monkeypatch, capsys):
    calls = []

    def fake_check_city_catalog_readiness(**kwargs):
        return CityReadinessResult(passed=True, city_count=10, warmup_ms=1.0, checked_cases=1)

    async def fake_validate_golden_cases():
        return [
            NatalAccuracyResult(
                case_id="case-1",
                passed=True,
                checked_points=26,
                externally_verified=True,
            )
        ]

    def fake_check_natal_config_readiness(settings, *, webhook_url):
        calls.append(("config", webhook_url))
        return NatalConfigReadinessResult(
            passed=False,
            status="not-ready",
            failures=["NATAL_REPORTS_ENABLED must be true for release readiness."],
        )

    async def fake_run_natal_smoke(webhook_url: str, user_id: int, chat_id: int):
        calls.append(("smoke", webhook_url, user_id, chat_id))
        raise AssertionError("smoke must not run after failed config readiness")

    monkeypatch.setattr("scripts.natal_readiness.check_city_catalog_readiness", fake_check_city_catalog_readiness)
    monkeypatch.setattr("scripts.natal_readiness.validate_golden_cases", fake_validate_golden_cases)
    monkeypatch.setattr("scripts.natal_readiness.check_natal_config_readiness", fake_check_natal_config_readiness)
    monkeypatch.setattr("scripts.natal_readiness.run_natal_smoke", fake_run_natal_smoke)

    exit_code = await natal_readiness._main(
        require_external=False,
        check_storage=False,
        webhook_url="https://bot.example.com",
        user_id=11,
        chat_id=22,
        max_city_warmup_ms=None,
        max_city_search_ms=None,
        min_city_count=30000,
        check_horizons=False,
        check_config=True,
        run_smoke=True,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert calls == [("config", "https://bot.example.com")]
    assert "FAIL natal-config: not-ready" in output
    assert "smoke_report_id" not in output
