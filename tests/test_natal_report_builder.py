import pytest

from app.natal.models import ChartData, InputQuality, NatalReport, PlanetPosition, ReportSection, TimePrecision
from app.natal.report_builder import build_hosted_report_html, build_telegraph_markdown


@pytest.fixture
def sample_natal_report() -> NatalReport:
    chart = ChartData(
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
    return NatalReport(
        report_id="abc",
        user_id=123,
        chart=chart,
        svg='<svg viewBox="0 0 10 10"></svg>',
        sections=[
            ReportSection(
                id="section-sun",
                title="Солнце",
                body_markdown="Натальная карта показывает Солнце в Водолее.",
                chart_refs=["sun"],
            )
        ],
    )


def test_hosted_report_contains_svg_and_section_ids(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)

    assert "<svg" in html
    assert 'id="section-sun"' in html
    assert "Натальная карта" in html


def test_telegraph_markdown_links_to_hosted_report(sample_natal_report: NatalReport):
    sample_natal_report.hosted_url = "https://example.com/reports/natal/abc"

    markdown = build_telegraph_markdown(sample_natal_report)

    assert "https://example.com/reports/natal/abc" in markdown
    assert "<svg" not in markdown
