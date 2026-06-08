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
    assert 'class="chart-stage"' in html
    assert 'class="highlights"' in html
    assert 'class="positions-grid"' in html


def test_hosted_report_credits_geonames_city_data(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)

    assert "GeoNames" in html
    assert "CC BY 4.0" in html
    assert "https://www.geonames.org/" in html


def test_hosted_report_places_full_interpretation_before_reference_positions(sample_natal_report: NatalReport):
    sample_natal_report.sections.extend(
        [
            ReportSection(
                id="section-moon",
                title="Луна",
                body_markdown="Эмоциональный ритм и потребности.",
                chart_refs=["moon"],
            ),
            ReportSection(
                id="section-aspects",
                title="Аспекты",
                body_markdown="Главные связи между планетами.",
                chart_refs=["sun", "moon"],
            ),
        ]
    )

    html = build_hosted_report_html(sample_natal_report)

    assert html.index('class="highlights"') < html.index('class="full-reading"')
    assert html.index('class="full-reading"') < html.index('class="positions-grid"')
    assert "Главные акценты" in html
    assert "Расчетные позиции" in html
    assert "Полный разбор" in html
    assert "Аспекты" in html


def test_hosted_report_labels_positions_with_user_facing_meaning(sample_natal_report: NatalReport):
    sample_natal_report.chart.planets.append(
        PlanetPosition(
            key="moon",
            label="Луна",
            longitude=120,
            sign="Лев",
            degree_in_sign=0,
        )
    )

    html = build_hosted_report_html(sample_natal_report)

    assert "Ядро личности" in html
    assert "Эмоции и потребности" in html
    assert "Планеты" not in html


def test_hosted_report_highlight_cards_use_readable_plain_text_previews(sample_natal_report: NatalReport):
    sample_natal_report.sections[0].body_markdown = (
        "**Солнце в Водолее** раскрывает [личный ритм](https://example.com) "
        "и помогает читать карту как цельную историю."
    )

    html = build_hosted_report_html(sample_natal_report)
    highlights_html = html.split('class="highlights"', 1)[1].split('class="full-reading"', 1)[0]

    assert ".highlight-card{display:block" in html
    assert "text-decoration:none" in html
    assert 'class="highlight-excerpt"' in highlights_html
    assert "<strong>Солнце в Водолее</strong>" not in highlights_html
    assert '<a href="https://example.com"' not in highlights_html
    assert "Солнце в Водолее раскрывает личный ритм" in highlights_html


def test_hosted_report_strips_javascript_urls_from_section_body(sample_natal_report: NatalReport):
    sample_natal_report.sections[0].body_markdown = "[опасная ссылка](javascript:alert(1))"

    html = build_hosted_report_html(sample_natal_report)

    assert "javascript:" not in html.lower()
    assert "опасная ссылка" in html


def test_hosted_report_ignores_unsafe_telegraph_url(sample_natal_report: NatalReport):
    sample_natal_report.telegraph_url = "javascript:alert(1)"

    html = build_hosted_report_html(sample_natal_report)

    assert "javascript:" not in html.lower()
    assert "Telegraph mirror" not in html


def test_hosted_report_ignores_insecure_telegraph_url(sample_natal_report: NatalReport):
    sample_natal_report.telegraph_url = "http://telegra.ph/natal-report"

    html = build_hosted_report_html(sample_natal_report)

    assert "http://telegra.ph/natal-report" not in html
    assert "Telegraph mirror" not in html


def test_hosted_report_sanitizes_stored_svg_payload(sample_natal_report: NatalReport):
    sample_natal_report.svg = '<svg><script>alert(1)</script><a href="javascript:alert(2)">x</a></svg>'

    html = build_hosted_report_html(sample_natal_report)

    assert "<svg" in html
    assert "<script" not in html.lower()
    assert "javascript:" not in html.lower()


def test_hosted_report_sanitizes_svg_event_handler_attributes(sample_natal_report: NatalReport):
    sample_natal_report.svg = '<svg onload="alert(1)"><circle onclick="alert(2)" cx="1" cy="1" r="1"/></svg>'

    html = build_hosted_report_html(sample_natal_report)

    assert "<svg" in html
    assert "onload=" not in html.lower()
    assert "onclick=" not in html.lower()
    assert "<circle" in html


def test_telegraph_markdown_links_to_hosted_report(sample_natal_report: NatalReport):
    sample_natal_report.hosted_url = "https://example.com/reports/natal/abc"

    markdown = build_telegraph_markdown(sample_natal_report)

    assert "https://example.com/reports/natal/abc" in markdown
    assert "<svg" not in markdown


def test_telegraph_markdown_credits_geonames_city_data(sample_natal_report: NatalReport):
    markdown = build_telegraph_markdown(sample_natal_report)

    assert "GeoNames" in markdown
    assert "CC BY 4.0" in markdown


def test_telegraph_markdown_excludes_interactive_content(sample_natal_report: NatalReport):
    sample_natal_report.sections[0].body_markdown = "<script>alert(1)</script>\n<svg></svg>\nText"

    markdown = build_telegraph_markdown(sample_natal_report)

    assert "<script" not in markdown.lower()
    assert "<svg" not in markdown.lower()
    assert "javascript:" not in markdown.lower()
