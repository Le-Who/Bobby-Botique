from __future__ import annotations

import html
import math
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from app.natal.destiny_matrix import render_destiny_matrix_svg
from app.natal.models import NatalReport, ReportSection, TimePrecision
from app.natal.svg_renderer import apply_chart_palette
from app.natal.text_safety import strip_user_facing_blocked_notes
from app.utils.text_format import markdown_to_html

_GEONAMES_ATTRIBUTION_HTML = (
    'Данные городов: <a href="https://www.geonames.org/" rel="noopener noreferrer">GeoNames</a>, CC BY 4.0.'
)
_GEONAMES_ATTRIBUTION_MARKDOWN = "Данные городов: GeoNames (https://www.geonames.org/), CC BY 4.0."

_POINT_MEANINGS = {
    "sun": "Ядро личности",
    "moon": "Эмоции и потребности",
    "mercury": "Мышление и речь",
    "venus": "Любовь и ценности",
    "mars": "Энергия и действие",
    "jupiter": "Рост и возможности",
    "saturn": "Границы и ответственность",
    "uranus": "Свобода и перемены",
    "neptune": "Интуиция и мечты",
    "pluto": "Глубина и трансформация",
}

_PLANET_SECTION_TARGETS = {
    "section-sun": "section-identity",
    "section-moon": "section-emotions",
    "section-mercury": "section-thinking",
    "section-venus": "section-love",
    "section-mars": "section-action",
    "section-jupiter": "section-growth",
    "section-saturn": "section-work-money",
    "section-uranus": "section-shadow-patterns",
    "section-neptune": "section-shadow-patterns",
    "section-pluto": "section-shadow-patterns",
}

_LEGACY_PERIOD_LINE_RE = re.compile(
    r"^-\s+\*\*(?P<age>[^—*]+?)\s+—\s+(?P<arcana>[^*]+)\*\*\.\s+"
    r"Возможные события периода:\s+(?P<events>.*?)\.\s+"
    r"Фокус десятилетия:\s+(?P<focus>.*?)\.\s+"
    r"Повторяющийся сюжет\s+—\s+(?P<theme>.*?);\s+"
    r"полезная стратегия\s+—\s+(?P<growth>.*?)\.?$"
)
_PERIOD_HEADING_RE = re.compile(r"^###\s+(?P<age>[^—\n]+?)\s+—\s+(?P<arcana>.+?)\s*$")
_PERIOD_FIELD_RE = re.compile(
    r"^\*\*(?P<label>Главный сюжет|Как это может проявиться|Теневой риск|Практичный ориентир)\.\*\*\s*"
    r"(?P<value>.*)$"
)
_PERIOD_FIELD_KEYS = {
    "Главный сюжет": "story",
    "Как это может проявиться": "manifestation",
    "Теневой риск": "shadow",
    "Практичный ориентир": "guidance",
}


def build_hosted_report_html(report: NatalReport, *, script_nonce: str = "") -> str:
    display_sections = _merge_related_sections(report.sections)
    display_section_ids = {section.id for section in display_sections}
    full_sections: list[tuple[ReportSection, str]] = []
    footer_notes: list[str] = []
    for index, section in enumerate(display_sections):
        section_id = html.escape(section.id, quote=True)
        title = html.escape(section.title)
        category_raw = _section_category(section)
        body_markdown, notes = _prepare_hosted_section_markdown(section)
        footer_notes.extend(notes)
        body = _sanitize_hosted_body(_hosted_section_body_html(section, body_markdown))
        category = html.escape(category_raw)
        default_open = ' open data-default-open="true"' if index == 0 else ""
        aliases = "".join(
            _section_anchor(alias, section.id) for alias in _section_aliases(section.id, display_section_ids)
        )
        full_sections.append(
            (
                section,
                f'{aliases}<details id="{section_id}" class="reading-card reading-disclosure" '
                f'data-category="{category}"{default_open}>'
                f'<summary><span class="summary-kicker">{category}</span>'
                f'<span class="summary-title"><strong>{title}</strong></span></summary>'
                f'<div class="reading-body">{body}</div></details>',
            )
        )

    telegraph = ""
    if report.telegraph_url and _is_safe_external_url(report.telegraph_url):
        url = html.escape(report.telegraph_url, quote=True)
        telegraph = f'<a class="mirror-link" href="{url}" rel="noopener noreferrer">Telegraph mirror</a>'

    positions = "".join(_position_cards(report, display_sections))
    notes_html = "".join(_footer_note_html(note) for note in footer_notes)
    title = _report_title(report)
    lead = _report_lead(report)
    visual_layers = _visual_layers(report)
    result_shell = _result_shell(report, title, lead, visual_layers, display_sections)
    reading_html = _full_reading_html(full_sections)
    positions_html = _positions_reference_html(positions)
    has_natal = bool(report.chart.planets)
    if has_natal:
        privacy = "Конфиденциальность: интерпретация строится по расчетным данным натальной карты."
    elif report.chart.destiny_matrix is not None:
        privacy = "Конфиденциальность: матрица рассчитана локально, без передачи данных языковой модели."
    else:
        privacy = "Конфиденциальность: в отчете показаны только доступные расчетные данные."
    attribution_html = f'<p class="attribution">{_GEONAMES_ATTRIBUTION_HTML}</p>' if has_natal else ""
    nonce_attr = f' nonce="{html.escape(script_nonce, quote=True)}"' if script_nonce else ""

    return (
        '<!doctype html><html lang="ru"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="icon" href="data:,">'
        f'<script src="https://telegram.org/js/telegram-web-app.js"{nonce_attr}></script>'
        f'<script src="/static/js/natal-theme.js"{nonce_attr}></script>'
        '<link rel="stylesheet" href="/static/css/natal-theme.css">'
        '<link rel="stylesheet" href="/static/css/natal-report.css">'
        "</head><body><main>"
        f"{result_shell}"
        '<section id="full-reading"><div class="section-head"><h2>Полный разбор</h2><p>Подробные интерпретации сгруппированы по смысловым категориям.</p></div>'
        f"{reading_html}</section>"
        f"{positions_html}"
        f'<footer class="footer">{telegraph}'
        f'<p class="privacy">{privacy}</p>{attribution_html}{notes_html}</footer>'
        f'</main><script defer src="/static/js/natal-report.js"{nonce_attr}></script></body></html>'
    )


def _full_reading_html(full_sections: list[tuple[ReportSection, str]]) -> str:
    natal_sections = [
        section_html for section, section_html in full_sections if not _is_destiny_reading_section(section)
    ]
    destiny_sections = [section_html for section, section_html in full_sections if _is_destiny_reading_section(section)]
    groups: list[str] = []
    if natal_sections:
        groups.append(
            _reading_group_html(
                "natal",
                "Натальная карта",
                "Разбор натальной карты",
                "Астрологические точки, аспекты и жизненные темы: как устроены реакции, выборы, близость и реализация.",
                natal_sections,
            )
        )
    if destiny_sections:
        groups.append(
            _reading_group_html(
                "destiny",
                "Матрица судьбы",
                "Разбор матрицы судьбы",
                "Архетипы даты рождения: центр, линии отношений и денег, родовые темы и возрастные периоды.",
                destiny_sections,
            )
        )
    return f'<div class="full-reading">{"".join(groups)}</div>'


def _reading_group_html(kind: str, kicker: str, title: str, description: str, sections: list[str]) -> str:
    escaped_kind = html.escape(kind, quote=True)
    heading_id = f"reading-group-{escaped_kind}-title"
    return (
        f'<section class="reading-group reading-group-{escaped_kind}" aria-labelledby="{heading_id}">'
        '<div class="reading-group-head">'
        f"<span>{html.escape(kicker)}</span>"
        f'<h3 id="{heading_id}">{html.escape(title)}</h3>'
        f"<p>{html.escape(description)}</p>"
        "</div>"
        f"{''.join(sections)}</section>"
    )


def _is_destiny_reading_section(section: ReportSection) -> bool:
    return section.id.lower().startswith("section-destiny") or "матриц" in section.title.lower()


def _positions_reference_html(positions: str) -> str:
    return (
        '<details id="positions" class="reference-disclosure reading-card">'
        '<summary><span class="summary-kicker">Справочный слой</span>'
        '<span class="summary-title"><strong>Расчетные позиции</strong></span></summary>'
        '<div class="reading-body reference-body">'
        "<p>Здесь собраны расчетные точки карты и матрицы. Это навигационный слой: его удобно открыть, "
        "когда хочется проверить, откуда взят конкретный вывод в разборе.</p>"
        f'<div class="positions-grid">{positions}</div></div></details>'
    )


def _prepare_hosted_section_markdown(section: ReportSection) -> tuple[str, list[str]]:
    markdown = strip_user_facing_blocked_notes(section.body_markdown)
    if not _is_aspect_section(section):
        return markdown, []

    markdown, notes = _extract_trailing_notes(markdown)
    return _separate_bold_blocks(markdown), notes


def _is_aspect_section(section: ReportSection) -> bool:
    section_id = section.id.lower()
    title = section.title.lower()
    return "aspect" in section_id or "аспект" in title or "внутренние связи" in title


def _extract_trailing_notes(markdown: str) -> tuple[str, list[str]]:
    match = re.search(
        r"(?is)(?:^|\s)(?:\*\*)?(?:примечание|note)(?:\*\*)?\s*[:：]\s*(?P<note>.+?)\s*$",
        markdown.strip(),
    )
    if not match:
        return markdown, []
    body = markdown[: match.start()].strip()
    note = match.group("note").strip()
    return body, [note] if note else []


def _separate_bold_blocks(markdown: str) -> str:
    stripped = markdown.strip()
    matches = list(re.finditer(r"\*\*[^*\n]{3,120}\*\*", stripped))
    if len(matches) < 2:
        return stripped

    blocks: list[str] = []
    preamble = stripped[: matches[0].start()].strip()
    if preamble:
        blocks.append(preamble)

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
        block = stripped[match.start() : end].strip()
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _hosted_markdown_to_html(markdown: str) -> str:
    converted = markdown_to_html(markdown).strip()
    if not converted:
        return ""

    blocks: list[str] = []
    for block in re.split(r"\n{2,}", converted):
        clean = block.strip()
        if not clean:
            continue
        if clean.startswith(("<p", "<pre", "<blockquote", "<ul", "<ol")):
            blocks.append(clean)
        else:
            blocks.append(f"<p>{clean}</p>")
    return "".join(blocks)


def _hosted_section_body_html(section: ReportSection, markdown: str) -> str:
    if section.id == "section-destiny-periods":
        return _destiny_periods_to_html(markdown)
    return _hosted_markdown_to_html(markdown)


def _destiny_periods_to_html(markdown: str) -> str:
    intro_lines: list[str] = []
    cards: list[str] = []
    current: dict[str, str] | None = None
    current_field: str | None = None

    def flush_current() -> None:
        nonlocal current, current_field
        if current and _period_card_is_complete(current):
            cards.append(_period_card_html(current))
        current = None
        current_field = None

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading_match = _PERIOD_HEADING_RE.match(line)
        if heading_match:
            flush_current()
            current = {
                "age": heading_match.group("age").strip(),
                "arcana": heading_match.group("arcana").strip(),
                "story": "",
                "manifestation": "",
                "shadow": "",
                "guidance": "",
            }
            continue

        if current is not None:
            field_match = _PERIOD_FIELD_RE.match(line)
            if field_match:
                current_field = _PERIOD_FIELD_KEYS[field_match.group("label")]
                current[current_field] = field_match.group("value").strip()
                continue
            if current_field:
                current[current_field] = f"{current[current_field]} {line}".strip()
                continue

        legacy_match = _LEGACY_PERIOD_LINE_RE.match(line)
        if legacy_match:
            flush_current()
            cards.append(_period_card_html(_legacy_period_parts(legacy_match.groupdict())))
            continue

        if current is None:
            intro_lines.append(line)
        elif current_field:
            current[current_field] = f"{current[current_field]} {line}".strip()

    flush_current()

    if not cards:
        return _hosted_markdown_to_html(markdown)

    intro_html = _hosted_markdown_to_html(" ".join(intro_lines))
    return f'{intro_html}<div class="period-list">{"".join(cards)}</div>'


def _period_card_html(parts: dict[str, str]) -> str:
    age = html.escape(parts["age"].strip())
    arcana = html.escape(parts["arcana"].strip())
    story = html.escape(parts["story"].strip())
    manifestation = html.escape(parts["manifestation"].strip())
    shadow = html.escape(parts["shadow"].strip())
    guidance = html.escape(parts["guidance"].strip())
    return (
        '<article class="period-card">'
        f"<header><span>{age}</span><strong>{arcana}</strong></header>"
        "<dl>"
        f"<div><dt>Главный сюжет</dt><dd>{story}</dd></div>"
        f"<div><dt>Как проявляется</dt><dd>{manifestation}</dd></div>"
        f"<div><dt>Теневой риск</dt><dd>{shadow}</dd></div>"
        f"<div><dt>Практичный ориентир</dt><dd>{guidance}</dd></div>"
        "</dl></article>"
    )


def _period_card_is_complete(parts: dict[str, str]) -> bool:
    required = ("age", "arcana", "story", "manifestation", "shadow", "guidance")
    return all(parts.get(key, "").strip() for key in required)


def _legacy_period_parts(parts: dict[str, str]) -> dict[str, str]:
    return {
        "age": parts["age"],
        "arcana": parts["arcana"],
        "story": parts["theme"],
        "manifestation": f"{parts['events']} Фокус десятилетия: {parts['focus']}",
        "shadow": parts["theme"],
        "guidance": parts["growth"],
    }


def _merge_related_sections(sections: list[ReportSection]) -> list[ReportSection]:
    by_id = {section.id: section for section in sections}
    merged: list[ReportSection] = []
    for section in sections:
        target_id = _PLANET_SECTION_TARGETS.get(section.id)
        if target_id and target_id in by_id:
            continue

        support_sections = [
            source
            for source_id, merge_target_id in _PLANET_SECTION_TARGETS.items()
            if merge_target_id == section.id and (source := by_id.get(source_id)) is not None
        ]
        if not support_sections:
            merged.append(section)
            continue

        support_markdown = "".join(_support_section_markdown(support) for support in support_sections)
        chart_refs = list(
            dict.fromkeys([*section.chart_refs, *(ref for support in support_sections for ref in support.chart_refs)])
        )
        merged.append(
            ReportSection(
                id=section.id,
                title=section.title,
                body_markdown=f"{section.body_markdown.rstrip()}{support_markdown}",
                chart_refs=chart_refs,
            )
        )
    return merged


def _support_section_markdown(section: ReportSection) -> str:
    title = _compact_support_title(section.title)
    return f"\n\n**Расчетная опора: {title}**\n\n{section.body_markdown.strip()}"


def _compact_support_title(title: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()


def _section_aliases(section_id: str, display_section_ids: set[str]) -> list[str]:
    aliases = [
        source_id
        for source_id, target_id in _PLANET_SECTION_TARGETS.items()
        if target_id == section_id and source_id not in display_section_ids
    ]
    return aliases


def _section_anchor(section_id: str, target_id: str) -> str:
    return (
        f'<span id="{html.escape(section_id, quote=True)}" class="section-anchor" '
        f'data-section-target="{html.escape(target_id, quote=True)}"></span>'
    )


def _footer_note_html(note: str) -> str:
    text = html.escape(_plain_text_excerpt(note, limit=1000))
    return f'<p class="report-note"><strong>Примечание:</strong> {text}</p>'


def _result_shell(report: NatalReport, title: str, lead: str, visual_layers: str, sections: list[ReportSection]) -> str:
    visual_html = f'<div class="visual-stack">{visual_layers}</div>' if visual_layers else ""
    lead_html = f'<p class="lead">{html.escape(lead)}</p>' if not report.chart.planets else ""
    overview_title = "Ваша карта" if report.chart.planets else "Ваша матрица"
    if not report.chart.planets and report.chart.destiny_matrix is None:
        overview_title = title
    return (
        '<header class="report-masthead"><span class="brand-mark" aria-hidden="true">☉</span>'
        f"<span>{html.escape(title)}</span></header>"
        '<nav class="report-nav" aria-label="Разделы результата">'
        '<a href="#overview" aria-current="location">Обзор</a>'
        '<a href="#full-reading">Разбор</a><a href="#positions">Положения</a></nav>'
        '<section id="overview" class="result-shell" aria-labelledby="overview-title">'
        '<div class="result-copy">'
        '<p class="eyebrow sr-only">Ваш результат уже готов</p>'
        f'<h1 id="overview-title">{html.escape(overview_title)}</h1>'
        f"{lead_html}"
        "</div>"
        f"{visual_html}"
        f"{_quick_positions_html(report, sections)}"
        f"{_calculation_note_html(report)}"
        f"{_reading_entry_html(sections)}"
        f"{_reading_path_html(report, sections)}"
        "</section>"
    )


def _quick_positions_html(report: NatalReport, sections: list[ReportSection]) -> str:
    section_ids = {section.id for section in sections}
    cards: list[str] = []
    symbols = {"sun": "☉", "moon": "☽"}
    for key in ("sun", "moon"):
        planet = next((point for point in report.chart.planets if point.key.lower() == key), None)
        if planet is None:
            continue
        raw_target = f"section-{key}"
        target = _PLANET_SECTION_TARGETS.get(raw_target, raw_target)
        reading_target: str | None = (
            target if target in section_ids else raw_target if raw_target in section_ids else None
        )
        cards.append(_quick_position_card(key, symbols[key], planet.label, planet.sign, reading_target))
    quality = report.chart.input_quality
    ascendant = report.chart.angles.get("ascendant")
    if (
        report.chart.planets
        and quality.angles_available
        and quality.time_precision != TimePrecision.UNKNOWN
        and ascendant is not None
        and math.isfinite(ascendant)
    ):
        signs = (
            "Овен",
            "Телец",
            "Близнецы",
            "Рак",
            "Лев",
            "Дева",
            "Весы",
            "Скорпион",
            "Стрелец",
            "Козерог",
            "Водолей",
            "Рыбы",
        )
        ascendant_target = next(
            (key for key in ("section-ascendant", "section-asc", "section-houses") if key in section_ids), None
        )
        cards.append(
            _quick_position_card("ascendant", "↑", "Асцендент", signs[int((ascendant % 360) // 30)], ascendant_target)
        )
    return f'<div class="quick-positions" aria-label="Основные позиции">{"".join(cards)}</div>' if cards else ""


def _quick_position_card(key: str, symbol: str, label: str, sign: str, target: str | None) -> str:
    tag = "a" if target else "article"
    href = f' href="#{html.escape(target, quote=True)}"' if target else ""
    return (
        f'<{tag} class="quick-position" data-point="{key}"{href}>'
        f'<span class="point-symbol" aria-hidden="true">{symbol}</span>'
        f'<span class="point-label">{html.escape(label)}</span>'
        f"<strong>{html.escape(sign)}</strong>"
        f"{'<span class="point-arrow" aria-hidden="true">›</span>' if target else ''}</{tag}>"
    )


def _calculation_note_html(report: NatalReport) -> str:
    if not report.chart.planets:
        return ""
    quality = report.chart.input_quality
    notes: list[str] = []
    if quality.time_precision == TimePrecision.UNKNOWN:
        notes.append("Время рождения неизвестно: дома и Асцендент не показаны.")
    elif quality.time_precision == TimePrecision.APPROXIMATE:
        notes.append("Время рождения примерное: дома и Асцендент приблизительны.")
    elif quality.time_precision == TimePrecision.RANGE:
        notes.append("Карта рассчитана по середине диапазона времени: дома и Асцендент приблизительны.")
    if quality.moon_uncertainty:
        notes.append("Знак и аспекты Луны могут меняться в течение дня.")
    return f'<p class="calculation-note">{html.escape(" ".join(notes))}</p>' if notes else ""


def _reading_entry_html(sections: list[ReportSection]) -> str:
    if not sections:
        return ""
    section = next((section for section in sections if section.id == "section-identity"), sections[0])
    return (
        f'<a class="reading-entry" href="#{html.escape(section.id, quote=True)}">'
        '<span class="entry-symbol" aria-hidden="true">✧</span><span class="entry-copy">'
        f"<strong>{html.escape(section.title)}</strong>"
        "<span>Полная интерпретация</span>"
        '<em>Читать разбор <span aria-hidden="true">→</span></em></span>'
        '<span class="entry-arrow" aria-hidden="true">›</span></a>'
    )


def _reading_path_html(report: NatalReport, sections: list[ReportSection]) -> str:
    has_natal = bool(report.chart.planets)
    has_matrix = report.chart.destiny_matrix is not None
    section_ids = {section.id for section in sections}
    natal_target = _first_natal_section_id(sections)
    matrix_target = "section-destiny-matrix" if "section-destiny-matrix" in section_ids else "full-reading"
    periods_target = "section-destiny-periods" if "section-destiny-periods" in section_ids else "positions"
    if has_natal and has_matrix:
        cards = [
            _path_card("1 шаг", natal_target, "Натальная карта", "Перейти к началу астрологического разбора."),
            _path_card("2 шаг", matrix_target, "Матрица судьбы", "Перейти к началу разбора матрицы."),
            _path_card("3 шаг", periods_target, "Возрастные периоды", "Посмотреть десятилетние акценты матрицы."),
        ]
    elif has_matrix:
        cards = [
            _path_card("1 шаг", matrix_target, "Матрица судьбы", "Перейти к началу разбора матрицы."),
            _path_card(
                "2 шаг",
                _target_section("section-destiny-money", section_ids),
                "Денежный канал",
                "Открыть практическую линию реализации.",
            ),
            _path_card("3 шаг", periods_target, "Возрастные периоды", "Посмотреть десятилетние акценты матрицы."),
        ]
    else:
        cards = [
            _path_card("1 шаг", natal_target, "Натальная карта", "Перейти к началу астрологического разбора."),
            _path_card("2 шаг", "full-reading", "Полный разбор", "Развернуть темы без потери контекста."),
            _path_card("3 шаг", "positions", "Расчетные позиции", "Проверить справочный слой карты."),
        ]
    return f'<nav class="reading-path" aria-label="Быстрые переходы">{"".join(cards)}</nav>'


def _path_card(step: str, target: str, title: str, subtitle: str) -> str:
    return (
        f'<a class="path-card" href="#{html.escape(target, quote=True)}"><span>{html.escape(step)}</span>'
        f"<strong>{html.escape(title)}</strong><em>{html.escape(subtitle)}</em></a>"
    )


def _first_natal_section_id(sections: list[ReportSection]) -> str:
    for section in sections:
        if not section.id.lower().startswith("section-destiny"):
            return section.id
    return "full-reading"


def _position_cards(report: NatalReport, sections: list[ReportSection]) -> list[str]:
    cards: list[str] = []
    section_ids = {section.id for section in sections}
    for planet in report.chart.planets:
        detail = f"{planet.sign} {planet.degree_in_sign:.1f}°"
        if planet.house:
            detail += f", дом {planet.house}"
        cards.append(
            _position_card(
                _planet_target_section(planet.key, section_ids),
                _point_meaning(planet.key),
                planet.label,
                detail,
            )
        )
    if report.chart.aspects:
        aspect_text = ", ".join(
            f"{aspect.point_a}-{aspect.point_b} {aspect.aspect}" for aspect in report.chart.aspects[:3]
        )
        cards.append(
            _position_card(
                _target_section("section-aspects", section_ids), "Внутренние связи", "Главные аспекты", aspect_text
            )
        )
    if report.chart.houses:
        house_text = ", ".join(f"{house.number}: {house.sign}" for house in report.chart.houses[:4])
        cards.append(
            _position_card(_target_section("section-houses", section_ids), "Сферы жизни", "Дома карты", house_text)
        )
    if report.chart.destiny_matrix:
        for position in report.chart.destiny_matrix.positions:
            if position.kind != "primary":
                continue
            cards.append(
                _position_card(
                    _destiny_target_section(position.key, section_ids),
                    "Матрица судьбы",
                    position.label,
                    f"{position.arcana}. {position.arcana_label}",
                )
            )
    if not cards:
        cards.append(
            _position_card(
                _target_section("section-summary", section_ids),
                "Расчетные данные",
                "Карта",
                "Расчетные точки будут доступны в полном разборе.",
            )
        )
    return cards


def _target_section(section_id: str, section_ids: set[str]) -> str:
    if section_id in section_ids:
        return section_id
    if "section-summary" in section_ids:
        return "section-summary"
    return min(section_ids, default="full-reading")


def _position_card(section_id: str, category: str, title: str, detail: str) -> str:
    return (
        f'<a class="position-card" href="#{html.escape(section_id, quote=True)}">'
        f"<span>{html.escape(category)}</span><strong>{html.escape(title)}</strong><p>{html.escape(detail)}</p></a>"
    )


def _planet_target_section(planet_key: str, section_ids: set[str]) -> str:
    raw_section_id = f"section-{planet_key.lower()}"
    target = _PLANET_SECTION_TARGETS.get(raw_section_id, raw_section_id)
    if target in section_ids:
        return target
    return _target_section(raw_section_id, section_ids)


def _section_category(section: ReportSection) -> str:
    section_id = section.id.lower()
    title = section.title.lower()
    if "destiny" in section_id or "матриц" in title:
        return "Матрица судьбы"
    if "work-money" in section_id or "деньг" in title or "работ" in title or "реализац" in title:
        return "Реализация"
    if "relationship" in section_id or "отнош" in title or "близост" in title:
        return "Отношения"
    if "shadow" in section_id or "тен" in title or "сценари" in title:
        return "Тени и рост"
    if "emotion" in section_id or "эмоци" in title or "восстанов" in title:
        return "Эмоции"
    if "thinking" in section_id or "мышлен" in title or "реч" in title:
        return "Мышление"
    if "love" in section_id or "любов" in title or "ценност" in title:
        return "Ценности"
    if "action" in section_id or "действ" in title or "конфликт" in title:
        return "Действие"
    if "growth" in section_id or "рост" in title:
        return "Практика"
    if "aspect" in section_id or "аспект" in title:
        return "Внутренние связи"
    if "house" in section_id or "дом" in title or "asc" in section_id or "mc" in section_id:
        return "Сферы жизни"
    if "summary" in section_id or "резюме" in title:
        return "Главное"
    point_key = section_id.removeprefix("section-")
    return _point_meaning(point_key)


def _point_meaning(point_key: str) -> str:
    return _POINT_MEANINGS.get(point_key.lower(), "Личная динамика")


def _excerpt(markdown: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", markdown).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _plain_text_excerpt(markdown: str, limit: int = 220) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"<[^>]+>", "", text)
    return _excerpt(html.unescape(text), limit)


def build_telegraph_markdown(report: NatalReport) -> str:
    display_sections = _merge_related_sections(report.sections)
    lines = [f"# {_report_title(report)}", ""]
    if report.hosted_url:
        lines.extend([f"Интерактивная версия: {report.hosted_url}", ""])
    if report.chart.planets:
        lines.extend(["## Планеты", "", "| Точка | Знак | Градус |", "|---|---:|---:|"])
        for planet in report.chart.planets:
            lines.append(f"| {planet.label} | {planet.sign} | {planet.degree_in_sign:.1f}° |")
    if report.chart.aspects:
        lines.extend(["", "## Аспекты", "", "| Точки | Аспект | Орб |", "|---|---:|---:|"])
        for aspect in report.chart.aspects:
            lines.append(f"| {aspect.point_a} - {aspect.point_b} | {aspect.aspect} | {aspect.orb:.1f}° |")
    if report.chart.destiny_matrix:
        lines.extend(["", "## Матрица судьбы", "", "| Позиция | Аркан | Тема |", "|---|---:|---|"])
        for position in report.chart.destiny_matrix.positions:
            if position.kind != "primary":
                continue
            lines.append(f"| {position.label} | {position.arcana}. {position.arcana_label} | {position.theme} |")
    for section in display_sections:
        lines.extend(["", f"## {section.title}", "", strip_user_facing_blocked_notes(section.body_markdown)])
    if report.chart.planets:
        lines.extend(["", _GEONAMES_ATTRIBUTION_MARKDOWN])
    markdown = "\n".join(lines)
    markdown = re.sub(r"<\s*/?\s*svg\b.*?>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
    markdown = re.sub(r"<\s*/?\s*script\b.*?>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
    markdown = re.sub(r"javascript:", "", markdown, flags=re.IGNORECASE)
    return markdown


def _sanitize_hosted_body(value: str) -> str:
    def clean_link(match: re.Match[str]) -> str:
        # Browsers discard tabs and newlines in URL schemes. Decode HTML once
        # (as the browser does) and normalize controls before checking it.
        url = re.sub(r"[\x00-\x20\x7f]+", "", html.unescape(match.group("url")))
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            return ""
        if scheme not in {"", "https", "http", "mailto", "tel", "tg"}:
            return ""
        return match.group(0)

    return re.sub(r'\s+href="(?P<url>[^"]*)"', clean_link, value, flags=re.IGNORECASE)


def _sanitize_hosted_svg(value: str) -> str:
    # Stored diagrams are untrusted. Permit the drawing vocabulary, never active
    # HTML, event handlers, CSS, or network resources (including SVG paint URLs).
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", value, flags=re.IGNORECASE):
        return ""
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return ""
    allowed_tags = {
        "svg",
        "g",
        "a",
        "title",
        "desc",
        "defs",
        "circle",
        "ellipse",
        "rect",
        "line",
        "polyline",
        "polygon",
        "path",
        "text",
        "tspan",
        "radialGradient",
        "linearGradient",
        "stop",
        "filter",
        "feGaussianBlur",
        "feMerge",
        "feMergeNode",
        "feDropShadow",
        "clipPath",
        "mask",
        "marker",
    }
    allowed_attrs = {
        "id",
        "class",
        "role",
        "aria-labelledby",
        "aria-label",
        "viewBox",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "transform",
        "fill",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-dasharray",
        "stroke-linecap",
        "stroke-linejoin",
        "opacity",
        "filter",
        "text-anchor",
        "dominant-baseline",
        "font-family",
        "font-size",
        "font-weight",
        "dx",
        "dy",
        "offset",
        "stop-color",
        "stop-opacity",
        "gradientUnits",
        "gradientTransform",
        "stdDeviation",
        "result",
        "in",
        "flood-color",
        "flood-opacity",
        "clip-path",
        "mask",
        "preserveAspectRatio",
        "href",
        "data-house",
        "data-aspect",
        "data-position",
        "data-kind",
        "marker-start",
        "marker-mid",
        "marker-end",
        "markerWidth",
        "markerHeight",
        "refX",
        "refY",
        "orient",
        "markerUnits",
    }
    paint_attrs = {"fill", "stroke", "stop-color", "flood-color"}
    local_ref = re.compile(r"#[a-zA-Z_][\w.-]*\Z")
    local_url = re.compile(r"url\(#[a-zA-Z_][\w.-]*\)\Z")
    safe_paint = re.compile(
        r"(?:#[0-9a-fA-F]{3,8}|none|currentColor|transparent|[a-zA-Z]+|var\(--(?:chart|matrix)-[a-z-]+,\s*#[0-9a-fA-F]{6}\))\Z"
    )

    def clean(element: ET.Element) -> None:
        element.tag = element.tag.rsplit("}", 1)[-1]
        for child in list(element):
            if child.tag.rsplit("}", 1)[-1] not in allowed_tags:
                element.remove(child)
            else:
                clean(child)
        attributes = dict(element.attrib)
        element.attrib.clear()
        for raw_name, attribute in attributes.items():
            name = raw_name.rsplit("}", 1)[-1]
            if name not in allowed_attrs:
                continue
            attribute = attribute.strip()
            if name == "href" and not local_ref.fullmatch(attribute):
                continue
            if name in {
                "filter",
                "clip-path",
                "mask",
                "marker-start",
                "marker-mid",
                "marker-end",
            } and not local_url.fullmatch(attribute):
                continue
            if name in paint_attrs and not (safe_paint.fullmatch(attribute) or local_url.fullmatch(attribute)):
                continue
            element.set(name, attribute)

    if root.tag.rsplit("}", 1)[-1] != "svg":
        return ""
    clean(root)
    root.set("xmlns", "http://www.w3.org/2000/svg")
    return apply_chart_palette(ET.tostring(root, encoding="unicode"))


def _is_safe_external_url(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("https://")


def _report_title(report: NatalReport) -> str:
    has_natal = bool(report.chart.planets)
    has_matrix = report.chart.destiny_matrix is not None
    if has_natal and has_matrix:
        return "Натальная карта и матрица судьбы"
    if has_matrix:
        return "Матрица судьбы"
    return "Натальная карта"


def _report_lead(report: NatalReport) -> str:
    has_natal = bool(report.chart.planets)
    has_matrix = report.chart.destiny_matrix is not None
    if has_natal and has_matrix:
        return (
            "Натальная карта и матрица по дате рождения. Откройте интересующую тему, "
            "чтобы прочитать её разбор и увидеть связанные расчётные позиции."
        )
    if has_matrix:
        return "Архетипическая матрица по дате рождения: центр, родовые линии, денежный канал, отношения и возрастные периоды."
    return "Откройте положение планеты или интересующую тему, чтобы прочитать её разбор."


def _visual_layers(report: NatalReport) -> str:
    layers: list[str] = []
    if report.svg.strip() and report.chart.planets:
        layers.append(_visual_stage_html("chart", "Натальная карта", report.svg))
    if report.chart.destiny_matrix is not None:
        matrix_svg = render_destiny_matrix_svg(report.chart.destiny_matrix)
        layers.append(_visual_stage_html("matrix", "Матрица судьбы", matrix_svg))
    if not layers and report.svg.strip():
        layers.append(_visual_stage_html("chart", "Натальная карта", report.svg))
    return "".join(layers)


def _visual_stage_html(kind: str, title: str, svg: str) -> str:
    diagram_id = f"{kind}-diagram"
    return (
        f'<div class="{kind}-stage"><div class="visual-tools"><span>{html.escape(title)}</span>'
        f'<button type="button" class="visual-zoom-toggle" aria-controls="{diagram_id}" '
        'aria-pressed="false" hidden>Увеличить схему</button></div>'
        f'<div id="{diagram_id}" class="visual-scroll" tabindex="0" '
        f'role="region" aria-label="{html.escape(title)}: схема с прокруткой">'
        f"{_sanitize_hosted_svg(svg)}</div></div>"
    )


def _destiny_target_section(position_key: str, section_ids: set[str]) -> str:
    target_by_position = {
        "center": "section-destiny-matrix",
        "portrait": "section-destiny-matrix",
        "higher_self": "section-destiny-spiritual",
        "soul_task": "section-destiny-socialization",
        "comfort": "section-destiny-comfort",
        "female_talent": "section-destiny-relationships",
        "money_channel": "section-destiny-money",
        "male_talent": "section-destiny-lineage",
        "karmic_tail": "section-destiny-lineage",
    }
    target = target_by_position.get(position_key, "section-destiny-matrix")
    if target in section_ids:
        return target
    return _target_section(target, section_ids)
