import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision
from app.natal.storage import get_report, mark_report_deleted, save_report


def make_report() -> NatalReport:
    return NatalReport(
        report_id="report-1",
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
        sections=[ReportSection(id="section-sun", title="Солнце", body_markdown="body")],
        hosted_url="https://example.com/reports/natal/report-1",
    )


@pytest.mark.asyncio
async def test_save_report_stores_payload(monkeypatch):
    calls = []

    async def fake_db_query(query, params=(), retries=3, conn=None):
        calls.append((query, params))
        return []

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    await save_report(make_report())

    query, params = calls[0]
    assert "INSERT INTO natal_reports" in query
    assert params[0] == "report-1"
    assert params[1] == 123
    assert params[3] == "<svg></svg>"


@pytest.mark.asyncio
async def test_get_report_returns_none_when_missing(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        return []

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    assert await get_report("missing") is None


@pytest.mark.asyncio
async def test_delete_report_marks_deleted(monkeypatch):
    async def fake_db_query(query, params=(), retries=3, conn=None):
        assert "UPDATE natal_reports" in query
        assert params == ("report-1", 123)
        return [{"report_id": "report-1"}]

    monkeypatch.setattr("app.natal.storage.db_query", fake_db_query)

    assert await mark_report_deleted("report-1", 123) is True
