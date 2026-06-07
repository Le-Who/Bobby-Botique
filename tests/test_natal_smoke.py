import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision
from app.natal.smoke import run_natal_smoke


@pytest.mark.asyncio
async def test_run_natal_smoke_creates_sample_report(monkeypatch):
    captured = {}

    async def fake_create_natal_report(birth_input, user_id, chat_id, webhook_url):
        captured["birth_input"] = birth_input
        captured["user_id"] = user_id
        captured["chat_id"] = chat_id
        captured["webhook_url"] = webhook_url
        return NatalReport(
            report_id="smoke-report",
            user_id=user_id,
            chart=ChartData(
                input_quality=InputQuality(
                    time_precision=TimePrecision.EXACT,
                    houses_available=True,
                    angles_available=True,
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
            sections=[ReportSection(id="section-sun", title="Солнце", body_markdown="body")],
            hosted_url=f"{webhook_url}/reports/natal/smoke-report",
        )

    monkeypatch.setattr("app.natal.smoke.create_natal_report", fake_create_natal_report)

    result = await run_natal_smoke(webhook_url="https://bot.example.com", user_id=1, chat_id=2)

    assert result.report_id == "smoke-report"
    assert result.hosted_url == "https://bot.example.com/reports/natal/smoke-report"
    assert captured["birth_input"].birth_place_display_name == "Odesa, Ukraine"
    assert captured["birth_input"].birth_place_timezone == "Europe/Kyiv"
    assert captured["user_id"] == 1
    assert captured["chat_id"] == 2
