import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision


@pytest.mark.asyncio
async def test_natal_report_route_returns_html(monkeypatch):
    from app.web import quart_app

    report = NatalReport(
        report_id="test-report-id",
        user_id=123,
        chart=ChartData(
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
        ),
        svg="<svg></svg>",
        sections=[ReportSection(id="section-sun", title="Солнце", body_markdown="Натальная карта")],
    )

    async def fake_get_report(report_id: str):
        assert report_id == "test-report-id"
        return report

    monkeypatch.setattr("app.web_natal.get_report", fake_get_report)
    quart_app.config["TESTING"] = True
    client = quart_app.test_client()

    response = await client.get("/reports/natal/test-report-id")

    assert response.status_code == 200
    body = await response.get_data(as_text=True)
    assert "<svg" in body
    assert "Натальная карта" in body
