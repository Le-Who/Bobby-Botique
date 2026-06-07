import pytest

from app.natal.models import BirthInput, ChartData, InputQuality, PlanetPosition, ReportSection, ResolvedBirthData, TimePrecision
from app.natal.service import create_natal_report


@pytest.mark.asyncio
async def test_create_natal_report_returns_hosted_url(monkeypatch):
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    async def fake_resolve_birth_data(birth_input):
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
        return None

    async def fake_create_telegraph_page_from_markdown(title, markdown_content):
        return None

    monkeypatch.setattr("app.natal.service.resolve_birth_data", fake_resolve_birth_data)
    monkeypatch.setattr("app.natal.service.calculate_chart", fake_calculate_chart)
    monkeypatch.setattr("app.natal.service.generate_interpretation", fake_generate_interpretation)
    monkeypatch.setattr("app.natal.service.render_chart_svg", lambda chart: "<svg></svg>")
    monkeypatch.setattr("app.natal.service.save_report", fake_save_report)
    monkeypatch.setattr("app.natal.service.create_telegraph_page_from_markdown", fake_create_telegraph_page_from_markdown)

    report = await create_natal_report(
        birth_input=birth,
        user_id=123,
        chat_id=456,
        webhook_url="https://bot.example.com",
    )

    assert report.report_id
    assert report.hosted_url.startswith("https://bot.example.com/reports/natal/")
    assert report.svg.startswith("<svg")
