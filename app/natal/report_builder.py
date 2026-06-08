from __future__ import annotations

import html
import re

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
    for section in report.sections:
        section_id = html.escape(section.id, quote=True)
        title = html.escape(section.title)
        body = _sanitize_hosted_body(markdown_to_html(section.body_markdown))
        category = html.escape(_section_category(section))
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

    return (
        "<!doctype html><html lang=\"ru\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Натальная карта</title>"
        "<style>"
        ":root{color-scheme:light;--ink:#2c2434;--muted:#73667d;--line:rgba(132,105,156,.18);--glass:rgba(255,255,255,.68);--rose:#d99caf;--violet:#8b75bd;--sky:#91b8df}"
        "*{box-sizing:border-box}html{scroll-behavior:smooth}"
        "body{margin:0;font-family:'Aptos','Segoe UI',sans-serif;background:radial-gradient(circle at 18% 8%,#fff8dc 0,#fff8dc 8%,transparent 25%),radial-gradient(circle at 82% 10%,#dceeff 0,transparent 28%),linear-gradient(145deg,#fff9f0 0%,#f7edff 48%,#eaf6ff 100%);color:var(--ink);line-height:1.65}"
        "body:before{content:'';position:fixed;inset:0;pointer-events:none;background:linear-gradient(120deg,rgba(255,255,255,.42),transparent 36%,rgba(255,255,255,.34));mix-blend-mode:screen}"
        "main{max-width:1120px;margin:0 auto;padding:clamp(20px,4vw,56px)}"
        ".hero{min-height:96vh;display:grid;align-content:center;gap:24px}"
        ".eyebrow{margin:0;color:var(--violet);font-size:13px;letter-spacing:.16em;text-transform:uppercase;font-weight:700}"
        "h1{max-width:760px;margin:0;font-family:Georgia,serif;font-size:clamp(42px,8vw,86px);line-height:.98;font-weight:500;color:#30253d}"
        ".lead{max-width:680px;margin:0;color:var(--muted);font-size:clamp(17px,2vw,21px)}"
        ".chart-stage{position:relative;margin:10px auto 0;width:min(760px,100%);padding:clamp(14px,3vw,28px);border:1px solid rgba(255,255,255,.78);border-radius:32px;background:linear-gradient(145deg,rgba(255,255,255,.72),rgba(255,255,255,.34));box-shadow:0 28px 80px rgba(107,83,139,.18),inset 0 1px 0 rgba(255,255,255,.9)}"
        ".chart-stage:before,.chart-stage:after{content:'';position:absolute;border-radius:999px;background:#fff;filter:blur(10px);opacity:.42}.chart-stage:before{width:110px;height:38px;left:8%;top:9%}.chart-stage:after{width:150px;height:46px;right:8%;bottom:10%}"
        "svg{position:relative;z-index:1;max-width:100%;height:auto;display:block;margin:0 auto}"
        ".section-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin:64px 0 18px}.section-head h2{margin:0;font-family:Georgia,serif;font-size:clamp(28px,4vw,44px);font-weight:500}.section-head p{max-width:480px;margin:0;color:var(--muted)}"
        ".highlights{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.highlight-card,.position-card,.reading-card{border:1px solid var(--line);background:var(--glass);backdrop-filter:blur(18px);box-shadow:0 18px 52px rgba(118,91,143,.12)}"
        ".highlight-card{display:block;min-height:180px;padding:22px;border-radius:24px;text-decoration:none;color:inherit;transition:transform .18s ease,box-shadow .18s ease}.highlight-card:hover{transform:translateY(-3px);box-shadow:0 22px 60px rgba(118,91,143,.18)}.highlight-card span,.reading-card span,.position-card span{display:block;margin-bottom:10px;color:var(--violet);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.highlight-card h3,.reading-card h3{margin:0 0 10px;font-family:Georgia,serif;font-size:24px;font-weight:500}.highlight-excerpt{margin:0;color:var(--muted)}"
        ".positions-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.position-card{display:block;min-height:138px;padding:18px;border-radius:20px;text-decoration:none;color:inherit;transition:transform .18s ease,box-shadow .18s ease}.position-card:hover{transform:translateY(-3px);box-shadow:0 22px 60px rgba(118,91,143,.18)}.position-card strong{display:block;margin-bottom:6px;font-family:Georgia,serif;font-size:22px;font-weight:500}.position-card p{margin:0;color:var(--muted)}"
        ".full-reading{display:grid;gap:18px}.reading-card{padding:clamp(20px,3vw,30px);border-radius:24px}.reading-card div{max-width:78ch}.reading-card p{margin:0 0 12px}.reading-card ul{padding-left:22px}"
        ".footer{display:flex;flex-wrap:wrap;gap:14px;margin:44px 0 0;color:var(--muted);font-size:14px}.mirror-link{color:#6f5b95;font-weight:700}.privacy,.attribution{margin:0}"
        "a{color:#6f5b95}@media(max-width:860px){.hero{min-height:auto}.highlights,.positions-grid{grid-template-columns:1fr}.section-head{display:block}.section-head p{margin-top:8px}.chart-stage{border-radius:24px}}"
        "</style></head><body><main>"
        '<section class="hero">'
        '<p class="eyebrow">Pastel cosmic reading</p>'
        "<h1>Натальная карта</h1>"
        '<p class="lead">Сначала карта как визуальный центр, затем главные акценты, полный разбор и справочные расчетные позиции.</p>'
        f'<div class="chart-stage">{_sanitize_hosted_svg(report.svg)}</div>'
        "</section>"
        '<section class="highlights-wrap"><div class="section-head"><h2>Главные акценты</h2><p>Самые важные и интересные мотивы вынесены первыми, чтобы отчет читался как цельная история.</p></div>'
        f'<div class="highlights">{highlights}</div></section>'
        '<section><div class="section-head"><h2>Полный разбор</h2><p>Подробные интерпретации сгруппированы по смысловым категориям.</p></div>'
        f'<div class="full-reading">{"".join(full_sections)}</div></section>'
        '<section><div class="section-head"><h2>Расчетные позиции</h2><p>Справочный слой карты: где находятся точки расчета и за какие темы они обычно отвечают.</p></div>'
        f'<div class="positions-grid">{positions}</div></section>'
        f'<footer class="footer">{telegraph}'
        '<p class="privacy">Privacy: LLM receives only derived chart data, not raw birth date or place.</p>'
        f'<p class="attribution">{_GEONAMES_ATTRIBUTION_HTML}</p></footer>'
        "</main></body></html>"
    )


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
    lines = ["# Натальная карта", ""]
    if report.hosted_url:
        lines.extend([f"Интерактивная версия: {report.hosted_url}", ""])
    lines.extend(["## Планеты", "", "| Точка | Знак | Градус |", "|---|---:|---:|"])
    for planet in report.chart.planets:
        lines.append(f"| {planet.label} | {planet.sign} | {planet.degree_in_sign:.1f}° |")
    if report.chart.aspects:
        lines.extend(["", "## Аспекты", "", "| Точки | Аспект | Орб |", "|---|---:|---:|"])
        for aspect in report.chart.aspects:
            lines.append(f"| {aspect.point_a} - {aspect.point_b} | {aspect.aspect} | {aspect.orb:.1f}° |")
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
