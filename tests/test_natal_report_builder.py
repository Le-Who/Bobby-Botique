import pytest

from app.natal.destiny_matrix import build_destiny_matrix_sections, calculate_destiny_matrix
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
    assert 'class="result-shell"' in html
    assert "Ваш результат уже готов" in html
    assert "Снимок разбора" not in html
    assert "Надёжность расчёта" not in html
    assert 'class="trust-box"' not in html
    assert 'class="natal-snapshot-grid"' not in html
    assert '<link rel="icon" href="data:,">' in html
    assert html.index('class="result-shell"') < html.index('class="chart-stage"')
    assert 'class="highlights"' not in html
    assert "Главные акценты" not in html
    assert "главные акценты" not in html.lower()
    assert 'class="positions-grid"' in html


def test_hosted_report_uses_expandable_thematic_sections_without_input_or_sales(sample_natal_report: NatalReport):
    sample_natal_report.sections.extend(
        [
            ReportSection(
                id="section-work-money",
                title="Работа, деньги и реализация",
                body_markdown="Практичный блок про выбор проектов и границы нагрузки.",
            ),
            ReportSection(
                id="section-shadow-patterns",
                title="Тени и повторяющиеся сценарии",
                body_markdown="Где человек может застревать и как это заметить без самобичевания.",
            ),
        ]
    )

    html = build_hosted_report_html(sample_natal_report)

    assert '<details id="section-sun"' in html
    assert '<summary><span>Ядро личности</span><strong>Солнце</strong></summary>' in html
    assert 'class="reading-body"' in html
    assert 'open data-default-open="true"' in html
    assert "Работа, деньги и реализация" in html
    assert "Тени и повторяющиеся сценарии" in html
    assert "Введите дату рождения" not in html
    assert "Тариф" not in html
    assert "оплат" not in html.lower()
    assert "бесплат" not in html.lower()


def test_hosted_report_renders_destiny_matrix_as_second_visual_layer(sample_natal_report: NatalReport):
    sample_natal_report.chart.destiny_matrix = calculate_destiny_matrix("1997-11-09")
    sample_natal_report.sections.append(
        ReportSection(
            id="section-destiny-matrix",
            title="Матрица судьбы",
            body_markdown="Матрица судьбы показывает архетипы даты рождения.",
        )
    )

    html = build_hosted_report_html(sample_natal_report)

    assert "Натальная карта и матрица судьбы" in html
    assert 'class="matrix-stage"' in html
    assert "Матрица судьбы" in html
    assert 'data-position="center"' in html
    assert html.index('class="chart-stage"') < html.index('class="matrix-stage"')


def test_hosted_report_path_links_to_natal_matrix_and_age_periods(sample_natal_report: NatalReport):
    sample_natal_report.chart.destiny_matrix = calculate_destiny_matrix("1997-11-09")
    sample_natal_report.sections.extend(build_destiny_matrix_sections(sample_natal_report.chart.destiny_matrix))

    html = build_hosted_report_html(sample_natal_report)
    path_html = html.split('class="reading-path"', 1)[1].split("</nav>", 1)[0]

    assert 'href="#section-sun"' in path_html
    assert "Натальная карта" in path_html
    assert 'href="#section-destiny-matrix"' in path_html
    assert "Матрица судьбы" in path_html
    assert 'href="#section-destiny-periods"' in path_html
    assert "Возрастные периоды" in path_html
    assert "Что читать первым" not in path_html
    assert "Главные акценты" not in path_html


def test_hosted_report_explains_destiny_matrix_positions_as_result_cards(sample_natal_report: NatalReport):
    sample_natal_report.chart.planets = []
    sample_natal_report.svg = ""
    sample_natal_report.chart.destiny_matrix = calculate_destiny_matrix("1997-11-09")
    sample_natal_report.sections = build_destiny_matrix_sections(sample_natal_report.chart.destiny_matrix)

    html = build_hosted_report_html(sample_natal_report)
    full_reading = html.split('class="full-reading"', 1)[1]

    assert "Матрица судьбы" in html
    assert 'class="matrix-insight-grid"' not in html
    assert "Снимок разбора" not in html
    assert "Ваша центральная энергия" in full_reading
    assert "портрет —" in full_reading
    assert "Денежный канал" in html
    assert "кармический хвост" in html
    assert "Архетипы показывают паттерны, а не фиксированную судьбу" not in html


def test_hosted_report_does_not_duplicate_natal_snapshot_before_full_text(sample_natal_report: NatalReport):
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

    assert 'class="natal-snapshot-grid"' not in html
    assert "Снимок разбора" not in html
    assert "Солнце" in html
    assert "Луна" in html


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

    assert html.index('class="full-reading"') < html.index('class="positions-grid"')
    assert "Главные акценты" not in html
    assert "главные акценты" not in html.lower()
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


def test_hosted_report_does_not_duplicate_full_text_as_highlight_previews(sample_natal_report: NatalReport):
    sample_natal_report.sections[0].body_markdown = (
        "**Солнце в Водолее** раскрывает [личный ритм](https://example.com) "
        "и помогает читать карту как цельную историю."
    )

    html = build_hosted_report_html(sample_natal_report)

    assert "Главные акценты" not in html
    assert "главные акценты" not in html.lower()
    assert 'class="highlights"' not in html
    assert 'class="highlight-excerpt"' not in html
    assert "Солнце в Водолее раскрывает личный ритм" not in html
    assert "<b>Солнце в Водолее</b>" in html


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


def test_hosted_report_formats_aspect_bold_blocks_as_separate_paragraphs(sample_natal_report: NatalReport):
    sample_natal_report.sections = [
        ReportSection(
            id="section-aspects",
            title="Внутренние связи",
            body_markdown=(
                "**Солнце квадрат Луна** напряжение между волей и потребностями. "
                "**Венера трин Марс** естественный обмен теплом и действием."
            ),
            chart_refs=["sun", "moon"],
        )
    ]

    html = build_hosted_report_html(sample_natal_report)
    aspect_html = html.split('id="section-aspects"', 1)[1].split("</details>", 1)[0]

    assert "<p><b>Солнце квадрат Луна</b> напряжение между волей и потребностями.</p>" in aspect_html
    assert "<p><b>Венера трин Марс</b> естественный обмен теплом и действием.</p>" in aspect_html


def test_hosted_report_moves_aspect_note_to_page_footer(sample_natal_report: NatalReport):
    sample_natal_report.sections = [
        ReportSection(
            id="section-aspects",
            title="Внутренние связи",
            body_markdown=(
                "**Солнце квадрат Луна** напряжение между волей и потребностями.\n\n"
                "Примечание: дома и углы не трактуются без точного времени."
            ),
        )
    ]

    html = build_hosted_report_html(sample_natal_report)
    aspect_html = html.split('id="section-aspects"', 1)[1].split("</details>", 1)[0]

    assert "Примечание:" not in aspect_html
    assert html.rfind("дома и углы не трактуются") > html.rfind('class="positions-grid"')
    assert 'class="report-note"' in html


def test_hosted_report_cards_do_not_use_backdrop_filter_for_scroll_stability(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)
    style = html.split("<style>", 1)[1].split("</style>", 1)[0]

    card_rule = next(rule for rule in style.split("}") if ".position-card,.reading-card" in rule)
    assert "backdrop-filter" not in card_rule
    assert "-webkit-backdrop-filter" not in card_rule


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
