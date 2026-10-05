import re
from pathlib import Path

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
    assert '<span class="summary-kicker">Ядро личности</span>' in html
    assert '<span class="summary-title"><strong>Солнце</strong></span>' in html
    assert 'class="reading-body"' in html
    assert 'open data-default-open="true"' in html
    assert "Работа, деньги и реализация" in html
    assert "Тени и повторяющиеся сценарии" in html
    assert "Введите дату рождения" not in html
    assert "Тариф" not in html
    assert "оплат" not in html.lower()
    assert "бесплат" not in html.lower()


def test_hosted_report_formats_destiny_periods_as_separate_cards(sample_natal_report: NatalReport):
    sample_natal_report.chart.planets = []
    sample_natal_report.svg = ""
    sample_natal_report.chart.destiny_matrix = calculate_destiny_matrix("2003-06-30")
    sample_natal_report.sections = build_destiny_matrix_sections(sample_natal_report.chart.destiny_matrix)

    html = build_hosted_report_html(sample_natal_report)
    period_html = html.split('id="section-destiny-periods"', 1)[1].split("</details>", 1)[0]

    assert 'class="period-list"' in period_html
    assert period_html.count('class="period-card"') == 8
    assert "<span>0-9 лет</span><strong>3. Императрица</strong>" in period_html
    assert "<dt>Главный сюжет</dt>" in period_html
    assert "<dt>Как проявляется</dt>" in period_html
    assert "<dt>Теневой риск</dt>" in period_html
    assert "<dt>Практичный ориентир</dt>" in period_html
    assert "- <b>0-9 лет" not in period_html


def test_hosted_report_merges_planet_sections_into_matching_life_topics(sample_natal_report: NatalReport):
    sample_natal_report.sections = [
        ReportSection(
            id="section-mercury",
            title="Меркурий — мышление и речь (Эмпатичный интеллект)",
            body_markdown="Меркурий в Раке дает эмоциональную память.",
            chart_refs=["mercury"],
        ),
        ReportSection(
            id="section-thinking",
            title="Мышление, речь и решения",
            body_markdown="Практичный блок про решения и разговоры.",
        ),
    ]

    html = build_hosted_report_html(sample_natal_report)

    assert '<details id="section-thinking"' in html
    assert '<details id="section-mercury"' not in html
    assert '<span id="section-mercury" class="section-anchor" data-section-target="section-thinking"></span>' in html
    assert "Расчетная опора: Меркурий — мышление и речь" in html
    assert "Эмпатичный интеллект" not in html.split("<summary>", 1)[1].split("</summary>", 1)[0]
    assert html.count('class="reading-card reading-disclosure"') == 1


def test_hosted_report_summary_has_stable_label_area_for_alignment(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)
    style = (Path(__file__).resolve().parents[1] / "app/static/css/natal-report.css").read_text(encoding="utf-8")

    assert 'class="summary-kicker"' in html
    assert 'class="summary-title"' in html
    assert '<link rel="stylesheet" href="/static/css/natal-report.css">' in html
    assert "grid-template-columns: minmax(112px,156px) minmax(0,1fr) 36px" in style
    assert ".summary-kicker { min-height: 44px" in style


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


def test_hosted_report_visually_groups_natal_and_destiny_reading_blocks(sample_natal_report: NatalReport):
    sample_natal_report.chart.destiny_matrix = calculate_destiny_matrix("1997-11-09")
    sample_natal_report.sections.extend(build_destiny_matrix_sections(sample_natal_report.chart.destiny_matrix))

    html = build_hosted_report_html(sample_natal_report)

    assert 'class="reading-group reading-group-natal"' in html
    assert 'class="reading-group reading-group-destiny"' in html
    assert "Разбор натальной карты" in html
    assert "Разбор матрицы судьбы" in html
    assert html.index('class="reading-group reading-group-natal"') < html.index(
        'class="reading-group reading-group-destiny"'
    )
    natal_group = html.split('class="reading-group reading-group-natal"', 1)[1].split("</section>", 1)[0]
    destiny_group = html.split('class="reading-group reading-group-destiny"', 1)[1].split("</section>", 1)[0]
    assert 'id="section-sun"' in natal_group
    assert 'id="section-destiny-matrix"' not in natal_group
    assert 'id="section-destiny-matrix"' in destiny_group


def test_hosted_report_hides_calculated_positions_in_expandable_reference_menu(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)

    assert '<details id="positions"' in html
    assert 'class="reference-disclosure' in html
    assert '<summary><span class="summary-kicker">Расчётные данные</span>' in html
    assert "Все положения" in html
    positions_html = html.split('<details id="positions"', 1)[1].split("</details>", 1)[0]
    assert 'class="positions-grid"' in positions_html
    assert '<section id="positions"' not in html


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


def test_matrix_only_report_does_not_claim_city_lookup_or_llm_processing(sample_natal_report: NatalReport):
    sample_natal_report.chart.planets = []
    sample_natal_report.svg = ""
    sample_natal_report.chart.destiny_matrix = calculate_destiny_matrix("1997-11-09")
    sample_natal_report.sections = build_destiny_matrix_sections(sample_natal_report.chart.destiny_matrix)

    html = build_hosted_report_html(sample_natal_report)
    markdown = build_telegraph_markdown(sample_natal_report)

    assert "GeoNames" not in html
    assert "GeoNames" not in markdown
    assert "LLM receives" not in html
    assert "без передачи данных языковой модели" in html


def test_empty_report_navigation_targets_exist(sample_natal_report: NatalReport):
    sample_natal_report.chart.planets = []
    sample_natal_report.svg = ""
    sample_natal_report.sections = []

    html = build_hosted_report_html(sample_natal_report)
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    targets = set(re.findall(r'href="#([^"]+)"', html))

    assert targets <= ids


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
    assert "Все положения" in html
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


@pytest.mark.parametrize("scheme", ["java\tscript", "java\nscript", "data"])
def test_hosted_report_removes_executable_links_even_with_url_control_characters(sample_natal_report, scheme):
    sample_natal_report.sections[0].body_markdown = f"[опасная ссылка]({scheme}:payload)"
    html = build_hosted_report_html(sample_natal_report)

    assert "опасная ссылка" in html
    assert not re.search(r'<a\s+href="[^"]*payload', html)


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
    chart_stage = html.split('class="chart-stage"', 1)[1]
    svg = chart_stage.split("<svg", 1)[1].split("</svg>", 1)[0]
    assert "<script" not in svg.lower()
    assert "javascript:" not in html.lower()


def test_hosted_report_sanitizes_svg_event_handler_attributes(sample_natal_report: NatalReport):
    sample_natal_report.svg = '<svg onload="alert(1)"><circle onclick="alert(2)" cx="1" cy="1" r="1"/></svg>'

    html = build_hosted_report_html(sample_natal_report)

    assert "<svg" in html
    assert "onload=" not in html.lower()
    assert "onclick=" not in html.lower()
    assert "<circle" in html


def test_report_overview_uses_real_positions_and_only_links_to_available_readings(sample_natal_report: NatalReport):
    sample_natal_report.chart.input_quality.angles_available = True
    sample_natal_report.chart.input_quality.houses_available = True
    sample_natal_report.chart.input_quality.time_precision = TimePrecision.EXACT
    sample_natal_report.chart.angles = {"ascendant": 166.5}
    html = build_hosted_report_html(sample_natal_report, script_nonce="test-nonce")
    overview = html.split('class="quick-positions"', 1)[1].split("</div>", 1)[0]

    assert 'data-point="sun"' in overview
    assert 'href="#section-sun"' in overview
    assert "Водолей" in overview
    assert 'data-point="ascendant"' in overview
    assert "Дева" in overview
    assert 'href="#section-ascendant"' not in overview
    assert 'href="#overview"' in html
    assert 'href="#full-reading"' in html
    assert 'href="#positions"' in html
    assert '<script src="/static/js/natal-theme.js" nonce="test-nonce"></script>' in html


def test_unknown_time_overview_omits_ascendant_even_if_stored_angles_exist(sample_natal_report: NatalReport):
    sample_natal_report.chart.angles = {"ascendant": 166.5}
    html = build_hosted_report_html(sample_natal_report)

    assert 'data-point="ascendant"' not in html
    assert "Время рождения неизвестно" in html
    assert "дома и Асцендент" in html


@pytest.mark.parametrize("precision", [TimePrecision.APPROXIMATE, TimePrecision.RANGE])
def test_imprecise_time_overview_labels_calculated_angles_as_approximate(sample_natal_report, precision):
    sample_natal_report.chart.input_quality.time_precision = precision
    sample_natal_report.chart.input_quality.angles_available = True
    sample_natal_report.chart.input_quality.houses_available = True
    sample_natal_report.chart.angles = {"ascendant": 166.5}
    html = build_hosted_report_html(sample_natal_report)

    assert 'data-point="ascendant"' in html
    assert "Дева" in html
    assert "приблизительны" in html


def test_stored_svg_cannot_load_external_resources_or_embed_active_html(sample_natal_report):
    sample_natal_report.svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><iframe src="https://evil.test"/>'
        '</foreignObject><image href="https://evil.test/track"/>'
        '<circle cx="1" cy="1" r="1" fill="url(https://evil.test/paint)"/>'
        '<a href="https://evil.test"><text x="1" y="1">Солнце</text></a></svg>'
    )
    html = build_hosted_report_html(sample_natal_report)
    chart_stage = html.split('class="chart-stage"', 1)[1]
    svg = chart_stage.split("<svg", 1)[1].split("</svg>", 1)[0]

    assert "evil.test" not in svg
    assert "foreignObject" not in svg
    assert "iframe" not in svg
    assert "<circle" in svg
    assert "Солнце" in svg


def test_stored_legacy_svg_retains_geometry_local_filters_and_navigation(sample_natal_report):
    sample_natal_report.svg = (
        '<svg viewBox="0 0 800 800" role="img" aria-labelledby="chart-title">'
        '<title id="chart-title">Натальная карта</title><defs><radialGradient id="paint">'
        '<stop offset="0%" stop-color="#fffdf8"/></radialGradient>'
        '<filter id="glow"><feGaussianBlur stdDeviation="5"/></filter></defs>'
        '<a href="#section-sun"><circle cx="265.2" cy="207.5" r="17" fill="url(#paint)" '
        'stroke="#6e5597" filter="url(#glow)"/></a></svg>'
    )
    html = build_hosted_report_html(sample_natal_report)
    chart_stage = html.split('class="chart-stage"', 1)[1]
    svg = chart_stage.split("<svg", 1)[1].split("</svg>", 1)[0]

    assert 'aria-labelledby="chart-title"' in svg
    assert 'cx="265.2" cy="207.5" r="17"' in svg
    assert 'fill="url(#paint)"' in svg
    assert 'filter="url(#glow)"' in svg
    assert 'href="#section-sun"' in svg
    assert 'stroke="var(--chart-planet-rim, #39add4)"' in svg


@pytest.mark.parametrize(
    "payload",
    [
        '<svg onload=alert(1)><circle r="1"/></svg>',
        '<!DOCTYPE svg [<!ENTITY x "unsafe">]><svg><text>&x;</text></svg>',
    ],
)
def test_malformed_or_entity_bearing_stored_svg_fails_closed(sample_natal_report, payload):
    sample_natal_report.svg = payload
    html = build_hosted_report_html(sample_natal_report)

    assert 'class="chart-stage"' not in html
    assert "unsafe" not in html
    assert "onload" not in html


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


def test_hosted_and_telegraph_reports_suppress_technical_natal_notes(sample_natal_report: NatalReport):
    technical_note = (
        "Техническое примечание: Этот разбор построен на основе расчетного движка ephem-local "
        "(без ручной валидации источника) и использует равнодомную систему от Асцендента. "
        "Полученные сетки домов и угловые точки (Асцендент, MC) — это качественные ориентиры."
    )
    sample_natal_report.sections[0].body_markdown = (
        f"{technical_note}\n\n"
        "Солнце в Водолее проще увидеть в жизни так: вам важно понимать, зачем вы участвуете в деле."
    )

    html = build_hosted_report_html(sample_natal_report)
    markdown = build_telegraph_markdown(sample_natal_report)
    combined = f"{html}\n{markdown}".lower()

    assert "солнце в водолее проще увидеть" in combined
    assert "техническое примечание" not in combined
    assert "ephem-local" not in combined
    assert "ручной валидации" not in combined
    assert "равнодом" not in combined
    assert "сетки домов" not in combined


def test_hosted_report_cards_do_not_use_backdrop_filter_for_scroll_stability(sample_natal_report: NatalReport):
    html = build_hosted_report_html(sample_natal_report)
    style = (Path(__file__).resolve().parents[1] / "app/static/css/natal-report.css").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/static/css/natal-report.css">' in html
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
