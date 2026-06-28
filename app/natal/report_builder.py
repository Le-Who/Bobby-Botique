from __future__ import annotations

import html
import re

from app.natal.destiny_matrix import render_destiny_matrix_svg
from app.natal.models import NatalReport, ReportSection
from app.utils.text_format import markdown_to_html

_GEONAMES_ATTRIBUTION_HTML = (
    'City data: <a href="https://www.geonames.org/" rel="noopener noreferrer">GeoNames</a>, CC BY 4.0.'
)
_GEONAMES_ATTRIBUTION_MARKDOWN = "City data: GeoNames (https://www.geonames.org/), CC BY 4.0."

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

_PERIOD_LINE_RE = re.compile(
    r"^-\s+\*\*(?P<age>[^—*]+?)\s+—\s+(?P<arcana>[^*]+)\*\*\.\s+"
    r"Возможные события периода:\s+(?P<events>.*?)\.\s+"
    r"Фокус десятилетия:\s+(?P<focus>.*?)\.\s+"
    r"Повторяющийся сюжет\s+—\s+(?P<theme>.*?);\s+"
    r"полезная стратегия\s+—\s+(?P<growth>.*?)\.?$"
)


def build_hosted_report_html(report: NatalReport) -> str:
    display_sections = _merge_related_sections(report.sections)
    display_section_ids = {section.id for section in display_sections}
    full_sections = []
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
        aliases = "".join(_section_anchor(alias) for alias in _section_aliases(section.id, display_section_ids))
        full_sections.append(
            f'{aliases}<details id="{section_id}" class="reading-card reading-disclosure" '
            f'data-category="{category}"{default_open}>'
            f'<summary><span class="summary-kicker">{category}</span>'
            f'<span class="summary-title"><strong>{title}</strong></span></summary>'
            f'<div class="reading-body">{body}</div></details>'
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

    return (
        "<!doctype html><html lang=\"ru\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="icon" href="data:,">'
        "<style>"
        ":root{color-scheme:light;--ink:#22302c;--muted:#66706c;--line:rgba(34,48,44,.14);--surface:#ffffff;--soft:#edf3f1;--teal:#0f766e;--blue:#285f9c;--amber:#b45309;--violet:#7557a6}"
        "*{box-sizing:border-box}html{scroll-behavior:smooth}"
        "body{margin:0;font-family:'Aptos','Segoe UI',sans-serif;background:#f4f7f5;color:var(--ink);line-height:1.62}"
        "main{position:relative;max-width:1160px;margin:0 auto;padding:clamp(16px,4vw,48px)}"
        ".result-shell{display:grid;gap:18px;margin:0 0 34px}"
        ".result-copy,.chart-stage,.matrix-stage,.position-card,.reading-card{border:1px solid var(--line);border-radius:8px;background:var(--surface);box-shadow:0 16px 42px rgba(33,45,42,.08)}"
        ".result-copy{padding:clamp(20px,4vw,34px);display:grid;gap:18px}"
        ".eyebrow{margin:0;color:var(--teal);font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:800}"
        "h1{max-width:780px;margin:0;font-family:Georgia,serif;font-size:clamp(36px,6vw,68px);line-height:1;font-weight:500;color:#1e2d29}"
        ".lead{max-width:720px;margin:0;color:var(--muted);font-size:clamp(16px,2vw,20px)}"
        ".reading-path{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.path-card{padding:12px;border:1px solid var(--line);border-radius:8px;text-decoration:none;color:inherit;background:var(--soft)}.path-card span{display:block;color:var(--teal);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.path-card strong{display:block;margin-top:4px;font-size:15px}.path-card em{display:block;margin-top:4px;color:var(--muted);font-size:13px;font-style:normal}"
        ".visual-stack{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:14px;align-items:start}"
        ".chart-stage,.matrix-stage{position:relative;width:100%;padding:clamp(12px,3vw,22px);overflow:hidden}"
        "svg{position:relative;z-index:1;max-width:100%;height:auto;display:block;margin:0 auto}"
        ".section-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:64px 0 18px}.section-head h2{margin:0;font-family:Georgia,serif;font-size:clamp(28px,4vw,44px);font-weight:500}.section-head p{max-width:480px;margin:0;color:var(--muted)}"
        ".position-card:hover{transform:translateY(-2px);box-shadow:0 18px 52px rgba(33,45,42,.12)}.summary-kicker,.position-card span{display:block;margin-bottom:0;color:var(--violet);font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.reading-card summary{list-style:none;cursor:pointer;padding:clamp(18px,3vw,26px);display:grid;grid-template-columns:minmax(112px,156px) minmax(0,1fr) 36px;gap:16px;align-items:center;min-height:116px}.reading-card summary::-webkit-details-marker{display:none}.reading-card summary:after{content:'+';width:36px;height:36px;border-radius:50%;display:grid;place-items:center;border:1px solid var(--line);font-size:24px;line-height:1;color:var(--teal);background:var(--soft);justify-self:end}.reading-card[open] summary:after{content:'−'}.summary-kicker{min-height:44px;display:flex;align-items:center;overflow-wrap:anywhere}.summary-title{display:block;min-width:0}.summary-title strong{font-family:Georgia,serif;font-size:clamp(23px,3vw,31px);line-height:1.18;font-weight:500;color:var(--ink)}"
        ".positions-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.position-card{display:block;min-height:132px;padding:16px;text-decoration:none;color:inherit;transition:transform .18s ease,box-shadow .18s ease}.position-card strong{display:block;margin-bottom:6px;font-family:Georgia,serif;font-size:21px;font-weight:500}.position-card p{margin:0;color:var(--muted)}"
        ".full-reading{display:grid;gap:14px}.reading-body{max-width:78ch;padding:0 clamp(18px,3vw,26px) clamp(18px,3vw,26px)}.reading-card p{margin:0 0 12px}.reading-card ul{padding-left:22px}"
        ".period-list{display:grid;gap:12px;margin-top:16px}.period-card{border:1px solid var(--line);border-radius:8px;background:var(--soft);padding:14px}.period-card header{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:baseline;margin:0 0 10px}.period-card header span{color:var(--teal);font-size:13px;font-weight:900;letter-spacing:.08em;text-transform:uppercase}.period-card header strong{font-family:Georgia,serif;font-size:22px;font-weight:500;color:var(--ink)}.period-card dl{display:grid;gap:9px;margin:0}.period-card dl div{display:grid;gap:2px}.period-card dt{color:var(--violet);font-size:11px;font-weight:900;letter-spacing:.1em;text-transform:uppercase}.period-card dd{margin:0;color:var(--ink)}.section-anchor{position:relative;top:-12px;display:block;height:0;overflow:hidden}"
        ".footer{display:flex;flex-wrap:wrap;gap:14px;margin:44px 0 0;color:var(--muted);font-size:14px}.mirror-link{color:var(--blue);font-weight:700}.privacy,.attribution{margin:0}.report-note{flex-basis:100%;margin:6px 0 0;padding-top:14px;border-top:1px solid var(--line)}.report-note strong{color:var(--ink)}"
        "a{color:var(--blue)}@media(max-width:900px){.reading-path,.positions-grid{grid-template-columns:1fr}.section-head{display:block}.section-head p{margin-top:8px}}@media(max-width:640px){main{padding:14px}.reading-card summary{grid-template-columns:minmax(96px,124px) minmax(0,1fr) 34px;gap:10px;min-height:104px}.summary-kicker{font-size:11px;letter-spacing:.13em;min-height:40px}.summary-title strong{font-size:clamp(21px,5vw,27px)}.reading-card summary:after{width:34px;height:34px;font-size:22px}}"
        "</style></head><body><main>"
        f"{result_shell}"
        '<section id="full-reading"><div class="section-head"><h2>Полный разбор</h2><p>Подробные интерпретации сгруппированы по смысловым категориям.</p></div>'
        f'<div class="full-reading">{"".join(full_sections)}</div></section>'
        '<section id="positions"><div class="section-head"><h2>Расчетные позиции</h2><p>Справочный слой карты: где находятся точки расчета и за какие темы они обычно отвечают.</p></div>'
        f'<div class="positions-grid">{positions}</div></section>'
        f'<footer class="footer">{telegraph}'
        '<p class="privacy">Privacy: LLM receives only derived chart data, not raw birth date or place.</p>'
        f'<p class="attribution">{_GEONAMES_ATTRIBUTION_HTML}</p>{notes_html}</footer>'
        "</main></body></html>"
    )


def _prepare_hosted_section_markdown(section: ReportSection) -> tuple[str, list[str]]:
    markdown = section.body_markdown
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
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _PERIOD_LINE_RE.match(line)
        if not match:
            intro_lines.append(line)
            continue
        cards.append(_period_card_html(match.groupdict()))

    if not cards:
        return _hosted_markdown_to_html(markdown)

    intro_html = _hosted_markdown_to_html(" ".join(intro_lines))
    return f'{intro_html}<div class="period-list">{"".join(cards)}</div>'


def _period_card_html(parts: dict[str, str]) -> str:
    age = html.escape(parts["age"].strip())
    arcana = html.escape(parts["arcana"].strip())
    events = html.escape(parts["events"].strip())
    focus = html.escape(parts["focus"].strip())
    theme = html.escape(parts["theme"].strip())
    growth = html.escape(parts["growth"].strip())
    return (
        '<article class="period-card">'
        f"<header><span>{age}</span><strong>{arcana}</strong></header>"
        "<dl>"
        f"<div><dt>Возможные события</dt><dd>{events}</dd></div>"
        f"<div><dt>Фокус десятилетия</dt><dd>{focus}</dd></div>"
        f"<div><dt>Повторяющийся сюжет</dt><dd>{theme}</dd></div>"
        f"<div><dt>Стратегия роста</dt><dd>{growth}</dd></div>"
        "</dl></article>"
    )


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
        chart_refs = list(dict.fromkeys([*section.chart_refs, *(ref for support in support_sections for ref in support.chart_refs)]))
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


def _section_anchor(section_id: str) -> str:
    return f'<span id="{html.escape(section_id, quote=True)}" class="section-anchor"></span>'


def _footer_note_html(note: str) -> str:
    text = html.escape(_plain_text_excerpt(note, limit=1000))
    return f'<p class="report-note"><strong>Примечание:</strong> {text}</p>'


def _result_shell(report: NatalReport, title: str, lead: str, visual_layers: str, sections: list[ReportSection]) -> str:
    visual_html = f'<div class="visual-stack">{visual_layers}</div>' if visual_layers else ""
    return (
        '<section class="result-shell">'
        '<div class="result-copy">'
        '<p class="eyebrow">Ваш результат уже готов</p>'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="lead">{html.escape(lead)}</p>'
        f"{_reading_path_html(report, sections)}"
        "</div>"
        f"{visual_html}"
        "</section>"
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
            _path_card("2 шаг", _target_section("section-destiny-money", section_ids), "Денежный канал", "Открыть практическую линию реализации."),
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
        aspect_text = ", ".join(f"{aspect.point_a}-{aspect.point_b} {aspect.aspect}" for aspect in report.chart.aspects[:3])
        cards.append(
            _position_card(_target_section("section-aspects", section_ids), "Внутренние связи", "Главные аспекты", aspect_text)
        )
    if report.chart.houses:
        house_text = ", ".join(f"{house.number}: {house.sign}" for house in report.chart.houses[:4])
        cards.append(_position_card(_target_section("section-houses", section_ids), "Сферы жизни", "Дома карты", house_text))
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
            _position_card("section-summary", "Расчетные данные", "Карта", "Расчетные точки будут доступны в полном разборе.")
        )
    return cards


def _target_section(section_id: str, section_ids: set[str]) -> str:
    if section_id in section_ids:
        return section_id
    if "section-summary" in section_ids:
        return "section-summary"
    return next(iter(section_ids), section_id)


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
        lines.extend(["", f"## {section.title}", "", section.body_markdown])
    lines.extend(["", _GEONAMES_ATTRIBUTION_MARKDOWN])
    markdown = "\n".join(lines)
    markdown = re.sub(r"<\s*/?\s*svg\b.*?>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
    markdown = re.sub(r"<\s*/?\s*script\b.*?>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
    markdown = re.sub(r"javascript:", "", markdown, flags=re.IGNORECASE)
    return markdown


def _sanitize_hosted_body(value: str) -> str:
    return re.sub(r"javascript\s*:", "", value, flags=re.IGNORECASE)


def _sanitize_hosted_svg(value: str) -> str:
    without_scripts = re.sub(r"<\s*script\b.*?<\s*/\s*script\s*>", "", value, flags=re.IGNORECASE | re.DOTALL)
    without_event_handlers = re.sub(r"\s+on[a-z0-9_-]+\s*=\s*(['\"]).*?\1", "", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"javascript\s*:", "", without_event_handlers, flags=re.IGNORECASE)


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
            "Сначала две визуальные карты: астрономический слой натала и архетипический слой матрицы. "
            "Дальше — разбор натальной карты, линий матрицы, денег, отношений, рода и возрастных периодов."
        )
    if has_matrix:
        return (
            "Архетипическая матрица по дате рождения: центр, родовые линии, денежный канал, отношения и возрастные периоды."
        )
    return "Сначала карта как визуальный центр, затем полный разбор и справочные расчетные позиции."


def _visual_layers(report: NatalReport) -> str:
    layers: list[str] = []
    if report.svg.strip() and report.chart.planets:
        layers.append(f'<div class="chart-stage">{_sanitize_hosted_svg(report.svg)}</div>')
    if report.chart.destiny_matrix is not None:
        matrix_svg = render_destiny_matrix_svg(report.chart.destiny_matrix)
        layers.append(f'<div class="matrix-stage">{_sanitize_hosted_svg(matrix_svg)}</div>')
    if not layers and report.svg.strip():
        layers.append(f'<div class="chart-stage">{_sanitize_hosted_svg(report.svg)}</div>')
    return "".join(layers)


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
