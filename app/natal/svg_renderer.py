from __future__ import annotations

import html
import math
import re

from app.natal.models import ChartData

_ZODIAC_SIGNS: tuple[tuple[str, str], ...] = (
    ("Овен", "♈"),
    ("Телец", "♉"),
    ("Близнецы", "♊"),
    ("Рак", "♋"),
    ("Лев", "♌"),
    ("Дева", "♍"),
    ("Весы", "♎"),
    ("Скорпион", "♏"),
    ("Стрелец", "♐"),
    ("Козерог", "♑"),
    ("Водолей", "♒"),
    ("Рыбы", "♓"),
)

_PLANET_SYMBOLS: dict[str, str] = {
    "sun": "☉",
    "moon": "☽",
    "mercury": "☿",
    "venus": "♀",
    "mars": "♂",
    "jupiter": "♃",
    "saturn": "♄",
    "uranus": "♅",
    "neptune": "♆",
    "pluto": "♇",
}


def render_chart_svg(chart: ChartData) -> str:
    center = 400
    radius = 280
    planet_radius = 235
    viewbox_height = 680 if chart.input_quality.houses_available else 720
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="60 60 680 {viewbox_height}" role="img" aria-labelledby="chart-title chart-desc">',
        '<title id="chart-title">Натальная карта</title>',
        '<desc id="chart-desc">Круговая схема планет, аспектов и домов.</desc>',
        "<defs>"
        '<radialGradient id="natal-bg" cx="50%" cy="42%" r="72%">'
        '<stop offset="0%" stop-color="#fffdf8"/><stop offset="58%" stop-color="#f7eefc"/>'
        '<stop offset="100%" stop-color="#dcecff"/></radialGradient>'
        '<filter id="soft-glow" x="-30%" y="-30%" width="160%" height="160%">'
        '<feGaussianBlur stdDeviation="5" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>'
        "</filter>"
        "</defs>",
        '<circle cx="400" cy="400" r="328" fill="url(#natal-bg)"/>',
        f'<circle cx="{center}" cy="{center}" r="{radius}" fill="#fffdfc" fill-opacity="0.92" stroke="#8a6ed0" stroke-width="3.2"/>',
        f'<circle cx="{center}" cy="{center}" r="{planet_radius}" fill="none" stroke="#9b87c9" stroke-width="1.8"/>',
        f'<circle cx="{center}" cy="{center}" r="{radius - 42}" fill="none" stroke="#d79aae" stroke-width="1.4"/>',
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
    return apply_chart_palette("".join(parts))


def apply_chart_palette(svg: str) -> str:
    """Theme drawing colors without changing stored coordinates or link targets.

    Fallback colors keep an exported standalone diagram readable. The hosted
    report supplies the dark Observatory or light Prism palette through CSS.
    The old renderer colors are also recognized for already stored diagrams.
    """
    colors = {
        "#fffdf8": ("glow", "#f7fdff"),
        "#f7eefc": ("glow-middle", "#e4f6fc"),
        "#dcecff": ("glow-edge", "#d6f2fa"),
        "#ffffff": ("surface", "#ffffff"),
        "#fffdfc": ("disk", "#effaff"),
        "#8a6ed0": ("rim", "#39add4"),
        "#9b87c9": ("ring", "#77b9cd"),
        "#d79aae": ("ring-soft", "#9bcddb"),
        "#7d68a8": ("ticks", "#517f96"),
        "#4d3a75": ("glyph", "#1d4156"),
        "#d4c6e2": ("houses", "#87b9ca"),
        "#7da7d9": ("harmonious", "#328bca"),
        "#d58cab": ("tense", "#d77985"),
        "#6e5597": ("planet-rim", "#39add4"),
        "#3f2e5c": ("planet", "#1d4156"),
        "#b45309": ("notice", "#765321"),
    }
    for color, (token, fallback) in colors.items():
        svg = svg.replace(f'="{color}"', f'="var(--chart-{token}, {fallback})"')
    # VS15 also repairs the presentation of legacy stored zodiac glyphs on
    # platforms whose default for these codepoints is a colored emoji tile.
    return re.sub(r"([♈♉♊♋♌♍♎♏♐♑♒♓])(?![︎️])", r"\1︎", svg)


def _render_zodiac_ticks(center: int, radius: int) -> list[str]:
    parts: list[str] = []
    for index in range(12):
        angle = math.radians(index * 30 - 90)
        x1 = center + math.cos(angle) * (radius - 18)
        y1 = center + math.sin(angle) * (radius - 18)
        x2 = center + math.cos(angle) * radius
        y2 = center + math.sin(angle) * radius
        label_angle = math.radians(index * 30 + 15 - 90)
        label_x = center + math.cos(label_angle) * (radius + 34)
        label_y = center + math.sin(label_angle) * (radius + 34)
        sign_name, sign_symbol = _ZODIAC_SIGNS[index]
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#7d68a8" stroke-width="1.4"/>'
        )
        parts.append(
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="middle" dominant-baseline="central" '
            'font-family="Segoe UI Symbol, Apple Symbols, Noto Sans Symbols 2, sans-serif" font-size="34" fill="#4d3a75">'
            f"<title>{html.escape(sign_name)}</title>{html.escape(sign_symbol)}</text>"
        )
    return parts


def _render_houses(chart: ChartData, center: int, radius: int) -> list[str]:
    parts: list[str] = []
    for house in chart.houses:
        angle = math.radians(house.cusp_longitude - 90)
        x = center + math.cos(angle) * radius
        y = center + math.sin(angle) * radius
        parts.append(
            f'<line data-house="{house.number}" x1="{center}" y1="{center}" x2="{x:.1f}" y2="{y:.1f}" '
            'stroke="#d4c6e2" stroke-width="1.1"/>'
        )
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
        color = "#7da7d9" if aspect.aspect in {"trine", "sextile"} else "#d58cab"
        parts.append(
            f'<line data-aspect="{html.escape(aspect.aspect, quote=True)}" x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="1.8" opacity="0.72"/>'
        )
    return parts


def _render_planets(chart: ChartData, center: int, radius: int) -> list[str]:
    parts: list[str] = []
    for planet in chart.planets:
        x, y = _point(center, radius, planet.longitude)
        label = html.escape(planet.label)
        section_id = html.escape(f"#section-{planet.key}", quote=True)
        symbol = html.escape(_PLANET_SYMBOLS.get(planet.key, planet.key[:2].upper()) + "︎")
        parts.append(f'<a href="{section_id}">')
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="21" fill="#ffffff" '
            'stroke="#6e5597" stroke-width="2.2" filter="url(#soft-glow)"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="central" '
            f'font-family="Segoe UI Symbol, Apple Symbols, Noto Sans Symbols 2, sans-serif" font-size="26" fill="#3f2e5c">{symbol}</text>'
        )
        parts.append(f"<title>{label} в знаке {html.escape(planet.sign)}</title></a>")
    return parts


def _render_legend(chart: ChartData) -> list[str]:
    return []


def _point(center: int, radius: int, longitude: float) -> tuple[float, float]:
    angle = math.radians(longitude - 90)
    return center + math.cos(angle) * radius, center + math.sin(angle) * radius
