from __future__ import annotations

import html
import re

from app.natal.models import NatalReport
from app.utils.text_format import markdown_to_html

_GEONAMES_ATTRIBUTION_HTML = (
    'City data: <a href="https://www.geonames.org/" rel="noopener noreferrer">GeoNames</a>, CC BY 4.0.'
)
_GEONAMES_ATTRIBUTION_MARKDOWN = "City data: GeoNames (https://www.geonames.org/), CC BY 4.0."


def build_hosted_report_html(report: NatalReport) -> str:
    sections = []
    for section in report.sections:
        section_id = html.escape(section.id, quote=True)
        title = html.escape(section.title)
        body = markdown_to_html(section.body_markdown)
        sections.append(f'<section id="{section_id}"><h2>{title}</h2><div>{body}</div></section>')

    telegraph = ""
    if report.telegraph_url:
        url = html.escape(report.telegraph_url, quote=True)
        telegraph = f'<p><a href="{url}" rel="noopener noreferrer">Telegraph mirror</a></p>'

    return (
        "<!doctype html><html lang=\"ru\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Натальная карта</title>"
        "<style>"
        "body{margin:0;font-family:Arial,sans-serif;background:#f8f6f0;color:#111827;line-height:1.55}"
        "main{max-width:960px;margin:0 auto;padding:24px}"
        "svg{max-width:100%;height:auto;display:block;margin:0 auto 24px}"
        "section{border-top:1px solid #d1d5db;padding:20px 0}"
        "h1,h2{line-height:1.2}a{color:#2563eb}.privacy,.attribution{font-size:14px;color:#4b5563}"
        "</style></head><body><main>"
        "<h1>Натальная карта</h1>"
        f"{report.svg}"
        f"{''.join(sections)}"
        f"{telegraph}"
        '<p class="privacy">Privacy: LLM receives only derived chart data, not raw birth date or place.</p>'
        f'<p class="attribution">{_GEONAMES_ATTRIBUTION_HTML}</p>'
        "</main></body></html>"
    )


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
