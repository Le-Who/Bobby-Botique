import pytest

from app.natal import service
from app.natal.models import (
    BirthInput,
    ChartData,
    InputQuality,
    PlanetPosition,
    ReportSection,
    ReportType,
    ResolvedBirthData,
    TimePrecision,
)
from app.natal.service import NatalConfigurationError, create_natal_report


def patch_report_dependencies(monkeypatch, *, telegraph_url=None, saved_reports=None, telegraph_enabled=False):
    captured = {"provider": None, "telegraph_calls": 0}

    async def fake_resolve_birth_data(birth_input, geocoder_provider=None):
        captured["provider"] = geocoder_provider
        return ResolvedBirthData(
            birth_input=birth_input,
            latitude=50.4501,
            longitude=30.5234,
            timezone="Europe/Kyiv",
            local_datetime="1995-02-14T12:00:00+02:00",
            utc_datetime="1995-02-14T10:00:00+00:00",
            display_place="Kyiv, Ukraine",
        )

    async def fake_calculate_chart(resolved):
        return ChartData(
            input_quality=InputQuality(
                time_precision=TimePrecision.UNKNOWN,
                houses_available=False,
                angles_available=False,
            ),
            planets=[
                PlanetPosition(
                    key="sun",
                    label="Солнце",
                    longitude=325,
                    sign="Водолей",
                    degree_in_sign=25,
                )
            ],
            aspects=[],
        )

    async def fake_generate_interpretation(chart, user_id, chat_id, language="ru", focus="general"):
        return [ReportSection(id="section-sun", title="Солнце", body_markdown="body")]

    async def fake_save_report(report):
        if saved_reports is not None:
            saved_reports.append(report.model_copy(deep=True))

    async def fake_create_telegraph_page_from_markdown(title, markdown_content):
        captured["telegraph_calls"] += 1
        return telegraph_url

    monkeypatch.setattr("app.natal.service.resolve_birth_data", fake_resolve_birth_data)
    monkeypatch.setattr("app.natal.service._natal_reports_enabled", lambda: True)
    monkeypatch.setattr("app.natal.service._natal_geocoder_provider", lambda: "local")
    monkeypatch.setattr("app.natal.service.calculate_chart", fake_calculate_chart)
    monkeypatch.setattr("app.natal.service.generate_interpretation", fake_generate_interpretation)
    monkeypatch.setattr("app.natal.service.render_chart_svg", lambda chart: "<svg></svg>")
    monkeypatch.setattr("app.natal.service.save_report", fake_save_report)
    monkeypatch.setattr(
        "app.natal.service.create_telegraph_page_from_markdown", fake_create_telegraph_page_from_markdown
    )
    monkeypatch.setattr(
        "app.natal.service._telegraph_publication_enabled",
        lambda: telegraph_enabled,
        raising=False,
    )
    return captured


@pytest.mark.asyncio
async def test_create_natal_report_returns_hosted_url(monkeypatch):
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    captured = patch_report_dependencies(monkeypatch)

    report = await create_natal_report(
        birth_input=birth,
        user_id=123,
        chat_id=456,
        webhook_url="https://bot.example.com",
    )

    assert report.report_id
    assert report.hosted_url.startswith("https://bot.example.com/reports/natal/")
    assert report.svg.startswith("<svg")
    assert captured["provider"] == "local"
    assert captured["telegraph_calls"] == 0


@pytest.mark.asyncio
async def test_create_natal_report_ignores_insecure_telegraph_url(monkeypatch):
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )
    saved_reports = []
    patch_report_dependencies(
        monkeypatch,
        telegraph_url="http://telegra.ph/insecure-natal-report",
        saved_reports=saved_reports,
        telegraph_enabled=True,
    )

    report = await create_natal_report(
        birth_input=birth,
        user_id=123,
        chat_id=456,
        webhook_url="https://bot.example.com",
    )

    assert report.telegraph_url is None
    assert len(saved_reports) == 1
    assert saved_reports[0].telegraph_url is None


def test_telegraph_url_validator_rejects_non_telegraph_https_host():
    from app.natal.service import _is_safe_telegraph_url

    assert _is_safe_telegraph_url("https://telegra.ph/valid-page") is True
    assert _is_safe_telegraph_url("https://evil.example/not-telegraph") is False
    assert _is_safe_telegraph_url("https://telegra.ph.evil.example/not-telegraph") is False


@pytest.mark.asyncio
async def test_create_natal_report_skips_oversized_telegraph_mirror(monkeypatch):
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )
    saved_reports = []
    patch_report_dependencies(monkeypatch, saved_reports=saved_reports, telegraph_enabled=True)
    monkeypatch.setattr("app.natal.service.build_telegraph_markdown", lambda report: "x" * 100_000)

    async def fail_if_called(title, markdown_content):
        raise AssertionError("Oversized Telegraph mirror should not be published")

    monkeypatch.setattr("app.natal.service.create_telegraph_page_from_markdown", fail_if_called)

    report = await create_natal_report(
        birth_input=birth,
        user_id=123,
        chat_id=456,
        webhook_url="https://bot.example.com",
    )

    assert report.hosted_url.startswith("https://bot.example.com/reports/natal/")
    assert report.telegraph_url is None
    assert len(saved_reports) == 1


@pytest.mark.asyncio
async def test_create_natal_report_respects_disabled_feature_flag(monkeypatch):
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    monkeypatch.setattr("app.natal.service._natal_reports_enabled", lambda: False)

    with pytest.raises(NatalConfigurationError, match="disabled"):
        await create_natal_report(
            birth_input=birth,
            user_id=123,
            chat_id=456,
            webhook_url="https://bot.example.com",
        )


def test_natal_reports_enabled_fails_closed_when_setting_is_missing(monkeypatch):
    class SettingsWithoutNatalFlag:
        pass

    monkeypatch.setattr("app.config.settings", SettingsWithoutNatalFlag())

    assert service._natal_reports_enabled() is False


@pytest.mark.asyncio
async def test_create_natal_report_can_include_destiny_matrix_without_extra_storage(monkeypatch):
    birth = BirthInput(
        birth_date="1997-11-09",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
        report_type=ReportType.COMBINED,
    )
    saved_reports = []
    patch_report_dependencies(monkeypatch, saved_reports=saved_reports)

    report = await create_natal_report(
        birth_input=birth,
        user_id=123,
        chat_id=456,
        webhook_url="https://bot.example.com",
    )

    assert report.chart.destiny_matrix is not None
    assert report.chart.destiny_matrix.birth_date == "1997-11-09"
    assert any(section.id == "section-destiny-matrix" for section in report.sections)
    assert saved_reports[0].chart.destiny_matrix is not None
