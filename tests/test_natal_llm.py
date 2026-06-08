from app.natal.llm import _fallback_sections, build_interpretation_prompt
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
