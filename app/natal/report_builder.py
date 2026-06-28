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


def build_hosted_report_html(report: NatalReport) -> str:
    full_sections = []
    footer_notes: list[str] = []
    for section in report.sections:
        section_id = html.escape(section.id, quote=True)
        title = html.escape(section.title)
        category_raw = _section_category(section)
        body_markdown, notes = _prepare_hosted_section_markdown(section)
        footer_notes.extend(notes)
        body = _sanitize_hosted_body(_hosted_markdown_to_html(body_markdown))
        category = html.escape(category_raw)
        full_sections.append(
            f'<article id="{section_id}" class="reading-card" data-category="{category}">'
            f"<span>{category}</span><h3>{title}</h3><div>{body}</div></article>"
        )

    telegraph = ""
    if report.telegraph_url and _is_safe_external_url(report.telegraph_url):
        url = html.escape(report.telegraph_url, quote=True)
        telegraph = f'<a class="mirror-link" href="{url}" rel="noopener noreferrer">Telegraph mirror</a>'

    highlights = "".join(_highlight_cards(report.sections))
    positions = "".join(_position_cards(report))
    notes_html = "".join(_footer_note_html(note) for note in footer_notes)
    title = _report_title(report)
    lead = _report_lead(report)
    visual_layers = _visual_layers(report)
    result_shell = _result_shell(report, title, lead, visual_layers)

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
        ".result-shell{display:grid;grid-template-columns:minmax(0,1.02fr) minmax(320px,.78fr);gap:18px;align-items:start;margin:0 0 34px}"
        ".result-copy,.result-panel,.chart-stage,.matrix-stage,.highlight-card,.position-card,.reading-card{border:1px solid var(--line);border-radius:8px;background:var(--surface);box-shadow:0 16px 42px rgba(33,45,42,.08)}"
        ".result-copy{padding:clamp(20px,4vw,34px);display:grid;gap:18px}.result-panel{padding:18px;display:grid;gap:16px}"
        ".eyebrow{margin:0;color:var(--teal);font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:800}"
        "h1{max-width:780px;margin:0;font-family:Georgia,serif;font-size:clamp(36px,6vw,68px);line-height:1;font-weight:500;color:#1e2d29}"
        ".lead{max-width:720px;margin:0;color:var(--muted);font-size:clamp(16px,2vw,20px)}"
        ".reading-path{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.path-card{padding:12px;border:1px solid var(--line);border-radius:8px;text-decoration:none;color:inherit;background:var(--soft)}.path-card span{display:block;color:var(--teal);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.path-card strong{display:block;margin-top:4px;font-size:15px}.path-card em{display:block;margin-top:4px;color:var(--muted);font-size:13px;font-style:normal}"
        ".panel-title{margin:0;font-size:18px;font-weight:760}.trust-box{padding:12px;border-left:4px solid var(--teal);background:var(--soft);border-radius:8px}.trust-box strong{display:block;margin-bottom:4px}.trust-box p{margin:0;color:var(--muted);font-size:14px}"
        ".natal-snapshot-grid,.matrix-insight-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.snapshot-card,.matrix-card{min-height:104px;padding:12px;border:1px solid var(--line);border-radius:8px;background:#fbfcfb}.snapshot-card span,.matrix-card span{display:block;color:var(--muted);font-size:12px;font-weight:750;letter-spacing:.08em;text-transform:uppercase}.snapshot-card strong,.matrix-card strong{display:block;margin-top:5px;font-size:18px}.snapshot-card p,.matrix-card p{margin:5px 0 0;color:var(--muted);font-size:13px}.matrix-note{grid-column:1/-1;margin:0;padding:10px 12px;border-radius:8px;background:#f7f1e8;color:#6b4a17;font-size:14px}"
        ".visual-stack{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:14px;align-items:start}"
        ".chart-stage,.matrix-stage{position:relative;width:100%;padding:clamp(12px,3vw,22px);overflow:hidden}"
        "svg{position:relative;z-index:1;max-width:100%;height:auto;display:block;margin:0 auto}"
        ".section-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:64px 0 18px}.section-head h2{margin:0;font-family:Georgia,serif;font-size:clamp(28px,4vw,44px);font-weight:500}.section-head p{max-width:480px;margin:0;color:var(--muted)}"
        ".highlights{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.highlight-card{display:block;min-height:170px;padding:18px;text-decoration:none;color:inherit;transition:transform .18s ease,box-shadow .18s ease}.highlight-card:hover,.position-card:hover{transform:translateY(-2px);box-shadow:0 18px 52px rgba(33,45,42,.12)}.highlight-card span,.reading-card span,.position-card span{display:block;margin-bottom:10px;color:var(--violet);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.highlight-card h3,.reading-card h3{margin:0 0 10px;font-family:Georgia,serif;font-size:23px;font-weight:500}.highlight-excerpt{margin:0;color:var(--muted)}"
        ".positions-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.position-card{display:block;min-height:132px;padding:16px;text-decoration:none;color:inherit;transition:transform .18s ease,box-shadow .18s ease}.position-card strong{display:block;margin-bottom:6px;font-family:Georgia,serif;font-size:21px;font-weight:500}.position-card p{margin:0;color:var(--muted)}"
        ".full-reading{display:grid;gap:14px}.reading-card{padding:clamp(18px,3vw,28px)}.reading-card div{max-width:78ch}.reading-card p{margin:0 0 12px}.reading-card ul{padding-left:22px}"
        ".footer{display:flex;flex-wrap:wrap;gap:14px;margin:44px 0 0;color:var(--muted);font-size:14px}.mirror-link{color:var(--blue);font-weight:700}.privacy,.attribution{margin:0}.report-note{flex-basis:100%;margin:6px 0 0;padding-top:14px;border-top:1px solid var(--line)}.report-note strong{color:var(--ink)}"
        "a{color:var(--blue)}@media(max-width:900px){.result-shell{grid-template-columns:1fr}.reading-path,.highlights,.positions-grid,.natal-snapshot-grid,.matrix-insight-grid{grid-template-columns:1fr}.section-head{display:block}.section-head p{margin-top:8px}}"
        "</style></head><body><main>"
        f"{result_shell}"
        '<section class="highlights-wrap" id="highlights"><div class="section-head"><h2>Главные акценты</h2><p>Самые важные и интересные мотивы вынесены первыми, чтобы отчет читался как цельная история.</p></div>'
        f'<div class="highlights">{highlights}</div></section>'
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


def _footer_note_html(note: str) -> str:
    text = html.escape(_plain_text_excerpt(note, limit=1000))
    return f'<p class="report-note"><strong>Примечание:</strong> {text}</p>'


def _result_shell(report: NatalReport, title: str, lead: str, visual_layers: str) -> str:
    visual_html = f'<div class="visual-stack">{visual_layers}</div>' if visual_layers else ""
    natal_snapshot = _natal_snapshot_grid(report)
    matrix_insights = _matrix_insight_grid(report)
    return (
        '<section class="result-shell">'
        '<div class="result-copy">'
        '<p class="eyebrow">Ваш результат уже готов</p>'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="lead">{html.escape(lead)}</p>'
        f"{_reading_path_html(report)}"
        "</div>"
        '<aside class="result-panel">'
        '<h2 class="panel-title">Снимок разбора</h2>'
        f"{_quality_box(report)}"
        f"{natal_snapshot}"
        f"{matrix_insights}"
        "</aside>"
        f"{visual_html}"
        "</section>"
    )


def _reading_path_html(report: NatalReport) -> str:
    has_matrix = report.chart.destiny_matrix is not None
    third_label = "Матрица и позиции" if has_matrix else "Расчетные позиции"
    return (
        '<nav class="reading-path" aria-label="Что читать первым">'
        '<a class="path-card" href="#highlights"><span>1 шаг</span><strong>Что читать первым</strong>'
        "<em>Коротко увидеть главные акценты отчета.</em></a>"
        '<a class="path-card" href="#full-reading"><span>2 шаг</span><strong>Полный разбор</strong>'
        "<em>Развернуть темы без потери контекста.</em></a>"
        f'<a class="path-card" href="#positions"><span>3 шаг</span><strong>{html.escape(third_label)}</strong>'
        "<em>Проверить расчетные точки и связать их с текстом.</em></a>"
        "</nav>"
    )


def _quality_box(report: NatalReport) -> str:
    quality = report.chart.input_quality
    if report.chart.destiny_matrix is not None and not report.chart.planets:
        text = "Матрица рассчитана по дате рождения; время и место в этом режиме не используются."
    elif not quality.houses_available or quality.time_precision == "unknown":
        text = "Время рождения неизвестно: планеты рассчитаны, но дома, Асцендент и MC не подаются как точные факты."
    elif quality.time_precision in {"approximate", "range"}:
        text = "Время задано не идеально точно: планеты устойчивы, а дома и углы читаются с пометкой о точности."
    else:
        text = "Дата, время и место позволяют читать планеты, аспекты, дома и углы как единый расчетный слой."
    if quality.warnings:
        text = f"{text} {_plain_text_excerpt(quality.warnings[0], limit=120)}"
    return f'<div class="trust-box"><strong>Надёжность расчёта</strong><p>{html.escape(text)}</p></div>'


def _natal_snapshot_grid(report: NatalReport) -> str:
    if not report.chart.planets:
        return ""
    cards: list[str] = []
    for planet in report.chart.planets[:4]:
        meaning = html.escape(_point_meaning(planet.key))
        title = html.escape(planet.label)
        detail = html.escape(f"{planet.sign} {planet.degree_in_sign:.1f}°")
        if planet.house:
            detail = f"{detail}, дом {planet.house}"
        cards.append(
            '<article class="snapshot-card">'
            f"<span>{meaning}</span><strong>{title}</strong><p>{detail}</p>"
            "</article>"
        )
    return f'<div class="natal-snapshot-grid">{"".join(cards)}</div>'


def _matrix_insight_grid(report: NatalReport) -> str:
    matrix = report.chart.destiny_matrix
    if matrix is None:
        return ""
    label_by_key = {
        "center": "Центр матрицы",
        "relationship": "Линия отношений",
        "money": "Денежный канал",
        "talent": "Таланты",
        "mission": "Предназначение",
    }
    by_key = {position.key: position for position in matrix.positions}
    cards: list[str] = []
    for key, label in label_by_key.items():
        position = by_key.get(key)
        if position is None:
            continue
        cards.append(
            '<article class="matrix-card">'
            f"<span>{html.escape(label)}</span>"
            f"<strong>{position.arcana}. {html.escape(position.arcana_label)}</strong>"
            f"<p>{html.escape(position.theme)}</p>"
            "</article>"
        )
    cards.append(
        '<p class="matrix-note">Архетипы показывают паттерны, а не фиксированную судьбу: '
        "этот слой лучше читать как язык выбора, ресурса и повторяющихся сценариев.</p>"
    )
    return f'<div class="matrix-insight-grid">{"".join(cards)}</div>'


def _highlight_cards(sections: list[ReportSection]) -> list[str]:
    source = sections[:3] or [
        ReportSection(id="section-summary", title="Краткое резюме", body_markdown="Разбор будет доступен ниже.")
    ]
    cards = []
    for section in source:
        title = html.escape(section.title)
        section_id = html.escape(section.id, quote=True)
        body = html.escape(_plain_text_excerpt(section.body_markdown))
        cards.append(
            f'<a class="highlight-card" href="#{section_id}"><span>{html.escape(_section_category(section))}</span>'
            f'<h3>{title}</h3><p class="highlight-excerpt">{body}</p></a>'
        )
    return cards


def _position_cards(report: NatalReport) -> list[str]:
    cards: list[str] = []
    section_ids = {section.id for section in report.sections}
    for planet in report.chart.planets:
        detail = f"{planet.sign} {planet.degree_in_sign:.1f}°"
        if planet.house:
            detail += f", дом {planet.house}"
        cards.append(
            _position_card(
                _target_section(f"section-{planet.key}", section_ids),
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


def _section_category(section: ReportSection) -> str:
    section_id = section.id.lower()
    title = section.title.lower()
    if "destiny" in section_id or "матриц" in title:
        return "Матрица судьбы"
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
            lines.append(f"| {position.label} | {position.arcana}. {position.arcana_label} | {position.theme} |")
    for section in report.sections:
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
            "Дальше — главные акценты, полный разбор и справочные расчетные позиции."
        )
    if has_matrix:
        return (
            "Архетипическая матрица по дате рождения: визуальный центр, главные акценты и расшифровка позиций без жестких предсказаний."
        )
    return "Сначала карта как визуальный центр, затем главные акценты, полный разбор и справочные расчетные позиции."


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
        "day": "section-destiny-matrix",
        "month": "section-destiny-matrix",
        "year": "section-destiny-matrix",
        "center": "section-destiny-matrix",
        "relationship": "section-destiny-relationship",
        "money": "section-destiny-money",
        "talent": "section-destiny-talents",
        "mission": "section-destiny-talents",
    }
    target = target_by_position.get(position_key, "section-destiny-matrix")
    if target in section_ids:
        return target
    return _target_section(target, section_ids)
