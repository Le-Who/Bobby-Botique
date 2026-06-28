from __future__ import annotations

import html
from datetime import date

from app.natal.models import DestinyMatrixData, DestinyMatrixPosition, ReportSection

_ARCANA_LABELS: dict[int, str] = {
    1: "Маг",
    2: "Верховная Жрица",
    3: "Императрица",
    4: "Император",
    5: "Иерофант",
    6: "Влюбленные",
    7: "Колесница",
    8: "Сила",
    9: "Отшельник",
    10: "Колесо Фортуны",
    11: "Справедливость",
    12: "Повешенный",
    13: "Смерть",
    14: "Умеренность",
    15: "Дьявол",
    16: "Башня",
    17: "Звезда",
    18: "Луна",
    19: "Солнце",
    20: "Суд",
    21: "Мир",
    22: "Шут",
}

_ARCANA_THEMES: dict[int, str] = {
    1: "инициатива, слово, личное мастерство",
    2: "интуиция, наблюдение, доверие внутреннему знанию",
    3: "созидание, забота, телесность и ресурс",
    4: "структура, опора, зрелые границы",
    5: "традиция, обучение, смысловые правила",
    6: "выбор, близость, честный диалог",
    7: "движение, воля, управление импульсом",
    8: "сила, мягкая власть, выдержка",
    9: "самостоятельность, исследование, внутренняя глубина",
    10: "циклы, гибкость, работа с переменами",
    11: "баланс, справедливость, договоренности",
    12: "пауза, смена взгляда, принятие ограничений",
    13: "обновление, завершение лишнего, трансформация",
    14: "умеренность, настройка ритма, исцеление через меру",
    15: "желания, власть, честность с зависимостями",
    16: "перестройка, освобождение от хрупких конструкций",
    17: "вдохновение, надежда, дальний ориентир",
    18: "чувствительность, сны, работа с тревогой",
    19: "ясность, тепло, проявленность",
    20: "призвание, родовая память, зрелый отклик",
    21: "целостность, завершение, большой контекст",
    22: "свобода, новый опыт, легкость старта",
}

_POSITION_LAYOUT: tuple[tuple[str, str, str, float, float], ...] = (
    ("day", "День рождения", "личный стиль и привычный способ входить в задачи", 400, 90),
    ("month", "Месяц", "эмоциональный фон и базовый ресурс", 122, 250),
    ("year", "Год", "социальная задача и внешний контекст", 678, 250),
    ("center", "Центр", "главный архетип сборки личности", 400, 345),
    ("relationship", "Линия отношений", "как строится близость и обмен поддержкой", 215, 558),
    ("money", "Денежный канал", "как раскрываются ценность, обмен и практический результат", 585, 558),
    ("talent", "Таланты", "естественные сильные стороны", 400, 625),
    ("mission", "Предназначение", "смысловой вектор без жестких обещаний", 400, 735),
)


def calculate_destiny_matrix(birth_date: str) -> DestinyMatrixData:
    parsed = _parse_birth_date(birth_date)
    day = _reduce_arcana(parsed.day)
    month = _reduce_arcana(parsed.month)
    year = _reduce_arcana(sum(int(char) for char in str(parsed.year)))
    center = _reduce_arcana(day + month + year)
    relationship = _reduce_arcana(day + center)
    money = _reduce_arcana(month + center)
    talent = _reduce_arcana(year + center)
    mission = _reduce_arcana(day + month + year + center)
    values = {
        "day": day,
        "month": month,
        "year": year,
        "center": center,
        "relationship": relationship,
        "money": money,
        "talent": talent,
        "mission": mission,
    }
    positions = [
        DestinyMatrixPosition(
            key=key,
            label=label,
            arcana=values[key],
            arcana_label=_ARCANA_LABELS[values[key]],
            theme=theme,
            x=x,
            y=y,
        )
        for key, label, theme, x, y in _POSITION_LAYOUT
    ]
    return DestinyMatrixData(
        birth_date=parsed.isoformat(),
        positions=positions,
        warnings=[
            "Матрица судьбы — нумерологическая архетипическая модель по дате рождения, а не астрономический расчет."
        ],
    )


def render_destiny_matrix_svg(matrix: DestinyMatrixData) -> str:
    by_key = {position.key: position for position in matrix.positions}
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 840" role="img" aria-labelledby="matrix-title matrix-desc">',
        '<title id="matrix-title">Матрица судьбы</title>',
        '<desc id="matrix-desc">Схема архетипов даты рождения по системе 22 арканов.</desc>',
        "<defs>"
        '<linearGradient id="matrix-bg" x1="0%" y1="0%" x2="100%" y2="100%">'
        '<stop offset="0%" stop-color="#fff7ed"/><stop offset="52%" stop-color="#ecfeff"/>'
        '<stop offset="100%" stop-color="#f0fdf4"/></linearGradient>'
        '<filter id="matrix-shadow" x="-20%" y="-20%" width="140%" height="140%">'
        '<feDropShadow dx="0" dy="12" stdDeviation="10" flood-color="#0f766e" flood-opacity=".16"/>'
        "</filter>"
        "</defs>",
        '<rect width="800" height="840" rx="34" fill="url(#matrix-bg)"/>',
        '<text x="400" y="48" text-anchor="middle" font-family="Georgia, serif" font-size="34" fill="#26352f">Матрица судьбы</text>',
    ]
    parts.extend(_matrix_line(by_key, "day", "center", "#0f766e"))
    parts.extend(_matrix_line(by_key, "month", "center", "#b45309"))
    parts.extend(_matrix_line(by_key, "year", "center", "#2563eb"))
    parts.extend(_matrix_line(by_key, "center", "relationship", "#be185d"))
    parts.extend(_matrix_line(by_key, "center", "money", "#047857"))
    parts.extend(_matrix_line(by_key, "center", "talent", "#7c3aed"))
    parts.extend(_matrix_line(by_key, "talent", "mission", "#475569"))
    for position in matrix.positions:
        parts.append(_matrix_node(position))
    parts.append(
        '<text x="400" y="812" text-anchor="middle" font-family="Aptos, Segoe UI, sans-serif" '
        'font-size="16" fill="#5f6f68">Архетипы помогают смотреть на паттерны, а не предсказывают фиксированную судьбу.</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def build_destiny_matrix_sections(matrix: DestinyMatrixData) -> list[ReportSection]:
    by_key = {position.key: position for position in matrix.positions}
    center = by_key["center"]
    relationship = by_key["relationship"]
    money = by_key["money"]
    talent = by_key["talent"]
    mission = by_key["mission"]
    return [
        ReportSection(
            id="section-destiny-matrix",
            title="Матрица судьбы — главные архетипы",
            body_markdown=(
                f"Центр матрицы — **{center.arcana}. {center.arcana_label}**: {center.theme}. "
                "Это слой для саморефлексии: он подсвечивает повторяющиеся способы выбора, общения и восстановления ресурса, "
                "но не заменяет личные решения."
            ),
            chart_refs=["destiny:center"],
        ),
        ReportSection(
            id="section-destiny-relationship",
            title="Линия отношений",
            body_markdown=(
                f"В отношениях активен архетип **{relationship.arcana}. {relationship.arcana_label}**: "
                f"{relationship.theme}. Практический фокус — замечать, где хочется автоматической реакции, "
                "и переводить ее в честный договор."
            ),
            chart_refs=["destiny:relationship"],
        ),
        ReportSection(
            id="section-destiny-money",
            title="Денежный канал",
            body_markdown=(
                f"В теме обмена и результата проявлен архетип **{money.arcana}. {money.arcana_label}**: "
                f"{money.theme}. Это не прогноз дохода, а подсказка о том, через какие качества легче создавать ценность."
            ),
            chart_refs=["destiny:money"],
        ),
        ReportSection(
            id="section-destiny-talents",
            title="Таланты и предназначение",
            body_markdown=(
                f"Таланты: **{talent.arcana}. {talent.arcana_label}** — {talent.theme}. "
                f"Смысловой вектор: **{mission.arcana}. {mission.arcana_label}** — {mission.theme}. "
                "Лучше читать это как язык сильных сторон и зон роста, без жестких обещаний."
            ),
            chart_refs=["destiny:talent", "destiny:mission"],
        ),
    ]


def _parse_birth_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Дата рождения должна быть в формате YYYY-MM-DD.") from exc


def _reduce_arcana(value: int) -> int:
    if value <= 0:
        return 22
    while value > 22:
        value -= 22
    return value or 22


def _matrix_line(by_key: dict[str, DestinyMatrixPosition], start: str, end: str, color: str) -> list[str]:
    first = by_key[start]
    second = by_key[end]
    return [
        f'<line x1="{first.x:.1f}" y1="{first.y:.1f}" x2="{second.x:.1f}" y2="{second.y:.1f}" '
        f'stroke="{color}" stroke-width="3" stroke-linecap="round" opacity=".34"/>'
    ]


def _matrix_node(position: DestinyMatrixPosition) -> str:
    label = html.escape(position.label)
    arcana_label = html.escape(position.arcana_label)
    theme = html.escape(position.theme)
    return (
        f'<g data-position="{html.escape(position.key, quote=True)}" filter="url(#matrix-shadow)">'
        f'<circle cx="{position.x:.1f}" cy="{position.y:.1f}" r="54" fill="#fffef9" stroke="#0f766e" stroke-width="2.2"/>'
        f'<text x="{position.x:.1f}" y="{position.y - 12:.1f}" text-anchor="middle" '
        'font-family="Georgia, serif" font-size="28" fill="#1f332c">'
        f"{position.arcana}</text>"
        f'<text x="{position.x:.1f}" y="{position.y + 14:.1f}" text-anchor="middle" '
        'font-family="Aptos, Segoe UI, sans-serif" font-size="13" font-weight="700" fill="#0f766e">'
        f"{arcana_label}</text>"
        f'<text x="{position.x:.1f}" y="{position.y + 82:.1f}" text-anchor="middle" '
        'font-family="Aptos, Segoe UI, sans-serif" font-size="14" fill="#44554e">'
        f"<title>{label}: {arcana_label} — {theme}</title>{label}</text>"
        "</g>"
    )
