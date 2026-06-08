from __future__ import annotations

import re

from app.natal.models import ChartData, ReportSection


def build_interpretation_prompt(chart: ChartData, language: str, focus: str) -> str:
    prompt_chart = _chart_for_prompt(chart)
    section_ids = ["section-summary", *(f"section-{planet.key}" for planet in chart.planets), "section-aspects"]
    confidence_rule = ""
    if not chart.input_quality.houses_available:
        confidence_rule = "Время неизвестно: не трактуй дома, Асцендент или MC как достоверные факты."
    quality_warnings = "\n".join(f"- {warning}" for warning in prompt_chart.input_quality.warnings)
    quality_block = (
        "Предупреждения качества:\n"
        f"- Движок расчета: {chart.input_quality.calculation_engine}.\n"
        f"- Reference validation: {chart.input_quality.reference_validated}.\n"
        f"{quality_warnings}\n"
        "Не подавай приблизительные дома, Асцендент или MC как полностью проверенные факты."
    )
    return (
        "Ты пишешь текстовую интерпретацию натальной карты по уже рассчитанным данным.\n"
        "Не запрашивай и не восстанавливай сырые дату рождения, место рождения или личные данные.\n"
        f"Язык ответа: {language or 'ru'}.\n"
        f"Фокус: {focus or 'general'}.\n"
        f"{confidence_rule}\n"
        f"{quality_block}\n"
        "Запрещены фаталистичные формулировки, медицинская, финансовая или юридическая определенность.\n"
        "Верни markdown-секции. Каждая секция должна начинаться заголовком вида `## section-id | Заголовок`.\n"
        f"Обязательные stable ids: {', '.join(section_ids)}.\n"
        "Для русского языка пиши кратко, глубоко и бережно.\n"
        "ChartData JSON:\n"
        f"{prompt_chart.model_dump_json()}"
    )


async def generate_interpretation(
    chart: ChartData,
    user_id: int,
    chat_id: int,
    language: str = "ru",
    focus: str = "general",
) -> list[ReportSection]:
    prompt = build_interpretation_prompt(chart, language, focus)
    try:
        from app.config import settings
        from app.providers import get_provider_router

        model = getattr(settings, "RESEARCH_MODEL", None) or getattr(settings, "DEFAULT_MODEL", "")
        if not model:
            return _fallback_sections(chart)
        router = get_provider_router()
        response, _tokens = await router.get_response(
            preferred_model=model,
            history=[{"role": "user", "parts": [prompt]}],
            user_id=user_id,
            chat_id=chat_id,
            timeout=60,
        )
        sections = _parse_sections(response or "")
        return sections or _fallback_sections(chart)
    except Exception:
        return _fallback_sections(chart)


def _parse_sections(markdown: str) -> list[ReportSection]:
    pattern = re.compile(r"^##\s+(section-[\w-]+)\s*\|\s*(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(markdown))
    sections: list[ReportSection] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        if body:
            sections.append(
                ReportSection(
                    id=match.group(1).strip(),
                    title=match.group(2).strip(),
                    body_markdown=body,
                )
            )
    return sections


def _fallback_sections(chart: ChartData) -> list[ReportSection]:
    planet_lines = [f"- {planet.label}: {planet.sign} {planet.degree_in_sign:.1f}°" for planet in chart.planets]
    aspect_lines = [f"- {aspect.point_a} {aspect.aspect} {aspect.point_b}, орб {aspect.orb:.1f}°" for aspect in chart.aspects]
    unavailable = "Интерпретация временно недоступна, поэтому ниже приведены только расчетные факты."
    quality_note = _quality_note(chart)
    sun = next((planet for planet in chart.planets if planet.key == "sun"), None)
    moon = next((planet for planet in chart.planets if planet.key == "moon"), None)
    return [
        ReportSection(
            id="section-summary",
            title="Краткое резюме",
            body_markdown=f"{unavailable}{quality_note}\n\n" + "\n".join(planet_lines[:10]),
        ),
        ReportSection(
            id="section-sun",
            title="Солнце",
            body_markdown=_planet_fact(sun, unavailable),
            chart_refs=["sun"],
        ),
        ReportSection(
            id="section-moon",
            title="Луна",
            body_markdown=_planet_fact(moon, unavailable),
            chart_refs=["moon"],
        ),
        ReportSection(
            id="section-aspects",
            title="Аспекты",
            body_markdown=f"{unavailable}\n\n" + ("\n".join(aspect_lines) if aspect_lines else "Мажорные аспекты не выделены."),
        ),
    ]


def _planet_fact(planet, unavailable: str) -> str:
    if planet is None:
        return unavailable
    return f"{unavailable}\n\n{planet.label}: {planet.sign} {planet.degree_in_sign:.1f}°."


def _quality_note(chart: ChartData) -> str:
    if not chart.input_quality.warnings:
        return ""
    warnings = "\n".join(f"- {_redact_raw_birth_data(warning)}" for warning in chart.input_quality.warnings)
    return f"\n\nПредупреждения качества:\n{warnings}"


def _chart_for_prompt(chart: ChartData) -> ChartData:
    return chart.model_copy(
        deep=True,
        update={
            "input_quality": chart.input_quality.model_copy(
                update={"warnings": [_redact_raw_birth_data(warning) for warning in chart.input_quality.warnings]}
            )
        },
    )


def _redact_raw_birth_data(value: str) -> str:
    if re.search(r"\b(?:birth_date|birth_place)\b|дата рождения|место рождения", value, flags=re.IGNORECASE):
        return "[redacted birth data]"
    redacted = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "[redacted date]", value)
    return re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", "[redacted date]", redacted)
