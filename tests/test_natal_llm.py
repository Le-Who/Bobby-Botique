from app.natal.llm import build_interpretation_prompt
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
