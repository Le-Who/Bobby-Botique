from types import SimpleNamespace

import pytest

from app.natal.llm import _fallback_sections, build_interpretation_prompt, generate_interpretation
from app.natal.models import ChartData, InputQuality, PlanetPosition, TimePrecision


def test_prompt_contains_confidence_rules_and_no_raw_birth_place():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN,
            houses_available=False,
            angles_available=False,
            warnings=["Время рождения неизвестно"],
        ),
        planets=[
            PlanetPosition(
                key="moon",
                label="Луна",
                longitude=120,
                sign="Лев",
                degree_in_sign=0,
            )
        ],
        aspects=[],
    )

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="general")

    assert "не трактуй дома" in prompt.lower()
    assert "section-moon" in prompt
    assert "Kyiv" not in prompt
    assert "1995" not in prompt


def test_prompt_requests_personality_domain_titles_for_planet_sections():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.EXACT,
            houses_available=True,
            angles_available=True,
        ),
        planets=[
            PlanetPosition(
                key="sun",
                label="Солнце",
                longitude=325,
                sign="Водолей",
                degree_in_sign=25,
            ),
            PlanetPosition(
                key="mercury",
                label="Меркурий",
                longitude=310,
                sign="Водолей",
                degree_in_sign=10,
            ),
        ],
        aspects=[],
    )

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="general")

    assert "Солнце — ядро личности" in prompt
    assert "Меркурий — мышление и речь" in prompt
    assert "не используй односложные заголовки" in prompt


def test_prompt_surfaces_quality_warnings_for_exact_time_heuristic_houses():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.EXACT,
            houses_available=True,
            angles_available=True,
            warnings=["Дома и Асцендент рассчитаны эвристически и требуют reference validation."],
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

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="general")

    assert "предупреждения качества" in prompt.lower()
    assert "эвристически" in prompt.lower()
    assert "не подавай приблизительные дома" in prompt.lower()


def test_prompt_redacts_raw_birth_details_from_quality_warnings():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.EXACT,
            houses_available=True,
            angles_available=True,
            warnings=[
                "birth_date=1995-02-14 birth_place=Kyiv, Ukraine",
                "Дата рождения: 14.02.1995; Место рождения: Одесса",
            ],
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

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="general")

    assert "1995-02-14" not in prompt
    assert "14.02.1995" not in prompt
    assert "Kyiv" not in prompt
    assert "Ukraine" not in prompt
    assert "Одесса" not in prompt
    assert "[redacted birth data]" in prompt


def test_fallback_sections_include_quality_warnings():
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.EXACT,
            houses_available=True,
            angles_available=True,
            warnings=["Дома рассчитаны эвристически."],
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

    sections = _fallback_sections(chart)

    assert "Дома рассчитаны эвристически" in sections[0].body_markdown


def test_fallback_sections_use_user_facing_planet_titles():
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
            ),
            PlanetPosition(
                key="moon",
                label="Луна",
                longitude=120,
                sign="Лев",
                degree_in_sign=0,
            ),
        ],
        aspects=[],
    )

    sections = _fallback_sections(chart)

    assert sections[1].title == "Солнце — ядро личности"
    assert sections[2].title == "Луна — эмоции и потребности"


@pytest.mark.asyncio
async def test_generate_interpretation_falls_back_when_llm_contradicts_calculated_sign(monkeypatch):
    chart = ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.EXACT,
            houses_available=True,
            angles_available=True,
        ),
        planets=[
            PlanetPosition(
                key="sun",
                label="Солнце",
                longitude=226.7,
                sign="Скорпион",
                degree_in_sign=16.7,
            )
        ],
        aspects=[],
    )

    class FakeRouter:
        async def get_response(self, **kwargs):
            del kwargs
            return (
                "## section-summary | Краткое резюме\n"
                "Солнце в Деве делает карту аналитичной.\n\n"
                "## section-sun | Солнце — ядро личности\n"
                "Солнце в Деве описывает аккуратность и порядок.",
                0,
            )

    monkeypatch.setattr("app.config.settings", SimpleNamespace(RESEARCH_MODEL="test-model", DEFAULT_MODEL=""))
    monkeypatch.setattr("app.providers.get_provider_router", lambda: FakeRouter())

    sections = await generate_interpretation(chart, user_id=123, chat_id=456)

    bodies = "\n".join(section.body_markdown for section in sections)
    assert "Деве" not in bodies
    assert "Скорпион" in bodies
