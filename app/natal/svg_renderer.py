from __future__ import annotations

import html
import math

from app.natal.models import ChartData


def render_chart_svg(chart: ChartData) -> str:
    center = 400
    radius = 280
    planet_radius = 235
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" role="img" aria-labelledby="chart-title chart-desc">',
        '<title id="chart-title">Натальная карта</title>',
        '<desc id="chart-desc">Круговая схема планет, аспектов и домов.</desc>',
        '<rect width="800" height="800" fill="#f8f6f0"/>',
        f'<circle cx="{center}" cy="{center}" r="{radius}" fill="#fffdf8" stroke="#1f2937" stroke-width="2"/>',
        f'<circle cx="{center}" cy="{center}" r="{planet_radius}" fill="none" stroke="#d1d5db" stroke-width="1"/>',
    ]
    parts.extend(_render_zodiac_ticks(center, radius))
    if chart.houses:
        parts.extend(_render_houses(chart, center, radius))
    parts.extend(_render_aspects(chart, center, planet_radius))
    parts.extend(_render_planets(chart, center, planet_radius))
    parts.extend(_render_legend(chart))
    if not chart.input_quality.houses_available:
        parts.append(
            '<text x="400" y="742" text-anchor="middle" font-family="Arial, sans-serif" '
            'font-size="18" fill="#b45309">Время рождения неизвестно: дома и Асцендент скрыты</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _render_zodiac_ticks(center: int, radius: int) -> list[str]:
    parts: list[str] = []
    for index in range(12):
        angle = math.radians(index * 30 - 90)
        x1 = center + math.cos(angle) * (radius - 18)
        y1 = center + math.sin(angle) * (radius - 18)
        x2 = center + math.cos(angle) * radius
        y2 = center + math.sin(angle) * radius
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#6b7280"/>')
    return parts


def _render_houses(chart: ChartData, center: int, radius: int) -> list[str]:
    parts: list[str] = []
    for house in chart.houses:
        angle = math.radians(house.cusp_longitude - 90)
        x = center + math.cos(angle) * radius
        y = center + math.sin(angle) * radius
        parts.append(f'<line x1="{center}" y1="{center}" x2="{x:.1f}" y2="{y:.1f}" stroke="#e5e7eb"/>')
    return parts


def _render_aspects(chart: ChartData, center: int, radius: int) -> list[str]:
    by_key = {planet.key: planet for planet in chart.planets}
    parts: list[str] = []
    for aspect in chart.aspects:
        first = by_key.get(aspect.point_a)
        second = by_key.get(aspect.point_b)
        if not first or not second:
            continue
        x1, y1 = _point(center, radius, first.longitude)
        x2, y2 = _point(center, radius, second.longitude)
        color = "#2563eb" if aspect.aspect in {"trine", "sextile"} else "#dc2626"
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="1.2" opacity="0.38"/>'
        )
    return parts


def _render_planets(chart: ChartData, center: int, radius: int) -> list[str]:
    parts: list[str] = []
    for planet in chart.planets:
        x, y = _point(center, radius, planet.longitude)
        label = html.escape(planet.label)
        section_id = html.escape(f"#section-{planet.key}", quote=True)
        parts.append(f'<a href="{section_id}">')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="15" fill="#111827"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" font-family="Arial, sans-serif" '
            f'font-size="11" fill="#ffffff">{html.escape(planet.key[:2].upper())}</text>'
        )
        parts.append(f'<title>{label} в знаке {html.escape(planet.sign)}</title></a>')
    return parts


def _render_legend(chart: ChartData) -> list[str]:
    parts = [
        '<text x="40" y="54" font-family="Arial, sans-serif" font-size="28" font-weight="700" fill="#111827">Натальная карта</text>',
    ]
    y = 92
    for planet in chart.planets[:10]:
        text = f"{planet.label}: {planet.sign} {planet.degree_in_sign:.1f}°"
        if planet.house:
            text += f", дом {planet.house}"
        parts.append(
            f'<text x="44" y="{y}" font-family="Arial, sans-serif" font-size="15" fill="#374151">'
            f"{html.escape(text)}</text>"
        )
        y += 22
    return parts


def _point(center: int, radius: int, longitude: float) -> tuple[float, float]:
    angle = math.radians(longitude - 90)
    return center + math.cos(angle) * radius, center + math.sin(angle) * radius
