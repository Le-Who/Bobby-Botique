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
    assert "section-emotions" in prompt
    assert "Kyiv" not in prompt
    assert "1995" not in prompt


def test_prompt_uses_planets_as_context_without_duplicate_planet_sections():
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

    assert "section-identity" in prompt
    assert "section-thinking" in prompt
    assert "section-sun" not in prompt
    assert "section-mercury" not in prompt
    assert "Не создавай отдельные секции по каждой планете" in prompt
    assert "Планеты используй внутри жизненных тем" in prompt


def test_prompt_requests_practical_expandable_life_topics_and_examples():
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
                key="venus",
                label="Венера",
                longitude=18,
                sign="Овен",
                degree_in_sign=18,
            ),
        ],
        aspects=[],
    )

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="general")

    assert "section-work-money" in prompt
    assert "section-shadow-patterns" in prompt
    assert "section-relationships" in prompt
    assert "практичные темы, которые человек может открывать по интересу" in prompt
    assert "жизненные примеры" in prompt
    assert "негативные стороны" in prompt
    assert "без лести" in prompt
    assert "не упоминай оплату" in prompt
    assert "не пиши, что разбор бесплатный" in prompt


def test_prompt_requests_personal_direct_non_mechanical_voice():
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
            )
        ],
        aspects=[],
    )

    prompt = build_interpretation_prompt(chart=chart, language="ru", focus="psychology")

    assert "обращайся к человеку напрямую" in prompt.lower()
    assert "на «вы»" in prompt.lower()
    assert "не звучать как справочник" in prompt.lower()
    assert "живой разбор конкретного человека" in prompt.lower()


@pytest.mark.asyncio
async def test_generate_interpretation_requests_key_retry_budget(monkeypatch):
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
            )
        ],
        aspects=[],
    )

    class FakeRouter:
        def __init__(self):
            self.calls = []

        async def get_response(self, **kwargs):
            self.calls.append(kwargs)
            return (
                "## section-summary | Краткое резюме\n"
                "Например, есть живой вектор. Теневая сторона названа.\n\n"
                "## section-identity | Ядро личности и способ проявляться\n"
                "Солнце в Водолее. Например, вы включаетесь через смысл. Теневая сторона — спор ради свободы.\n\n"
                "## section-emotions | Эмоциональные потребности и восстановление\n"
                "Например, нужен ритм. Теневая сторона — игнорировать усталость.\n\n"
                "## section-thinking | Мышление, речь и решения\n"
                "Например, помогает структура. Теневая сторона — спорить вместо уточнения.\n\n"
                "## section-love | Любовь, симпатия и личные ценности\n"
                "Например, важна честность. Теневая сторона — холодность.\n\n"
                "## section-action | Действие, конфликт и энергия\n"
                "Например, нужна цель. Теневая сторона — резкость.\n\n"
                "## section-work-money | Работа, деньги и реализация\n"
                "Например, результат держится на роли. Теневая сторона — распыление.\n\n"
                "## section-shadow-patterns | Тени и повторяющиеся сценарии\n"
                "Например, защита может стать привычкой. Теневая сторона видна в повторе.\n\n"
                "## section-relationships | Отношения и близость\n"
                "Например, важен прямой разговор. Теневая сторона — молчаливые проверки.\n\n"
                "## section-growth | Вектор роста и практичные шаги\n"
                "Например, помогает один шаг. Теневая сторона — ждать идеала.",
                0,
            )

    router = FakeRouter()
    monkeypatch.setattr("app.config.settings", SimpleNamespace(RESEARCH_MODEL="test-model", DEFAULT_MODEL=""))
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)

    sections = await generate_interpretation(chart, user_id=123, chat_id=456)

    assert sections
    assert router.calls[0]["max_key_retries"] >= 2
    assert router.calls[0]["timeout"] <= 45


@pytest.mark.asyncio
async def test_generate_interpretation_uses_current_primary_model_even_when_research_env_is_legacy(monkeypatch):
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
            )
        ],
        aspects=[],
    )

    class FakeRouter:
        def __init__(self):
            self.calls = []

        async def get_response(self, **kwargs):
            self.calls.append(kwargs)
            return (
                "## section-summary | Краткое резюме\n"
                "Например, есть живой вектор. Теневая сторона названа.\n\n"
                "## section-identity | Ядро личности и способ проявляться\n"
                "Солнце в Водолее. Например, вы включаетесь через смысл. Теневая сторона — спор ради свободы.\n\n"
                "## section-emotions | Эмоциональные потребности и восстановление\n"
                "Например, нужен ритм. Теневая сторона — игнорировать усталость.\n\n"
                "## section-thinking | Мышление, речь и решения\n"
                "Например, помогает структура. Теневая сторона — спорить вместо уточнения.\n\n"
                "## section-love | Любовь, симпатия и личные ценности\n"
                "Например, важна честность. Теневая сторона — холодность.\n\n"
                "## section-action | Действие, конфликт и энергия\n"
                "Например, нужна цель. Теневая сторона — резкость.\n\n"
                "## section-work-money | Работа, деньги и реализация\n"
                "Например, результат держится на роли. Теневая сторона — распыление.\n\n"
                "## section-shadow-patterns | Тени и повторяющиеся сценарии\n"
                "Например, защита может стать привычкой. Теневая сторона видна в повторе.\n\n"
                "## section-relationships | Отношения и близость\n"
                "Например, важен прямой разговор. Теневая сторона — молчаливые проверки.\n\n"
                "## section-growth | Вектор роста и практичные шаги\n"
                "Например, помогает один шаг. Теневая сторона — ждать идеала.",
                0,
            )

    router = FakeRouter()
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(RESEARCH_MODEL="gemini-3-flash-preview", DEFAULT_MODEL="gemini-3-flash-preview"),
    )
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)

    sections = await generate_interpretation(chart, user_id=123, chat_id=456)

    assert sections
    assert router.calls[0]["preferred_model"] == "gemini-3.5-flash"


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
    ids = {section.id for section in sections}
    titles = [section.title for section in sections]

    assert "section-sun" not in ids
    assert "section-moon" not in ids
    assert "Ядро личности и способ проявляться" in titles
    assert "Эмоциональные потребности и восстановление" in titles


def test_fallback_sections_include_practical_topics_with_examples_and_shadow_language():
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
            PlanetPosition(
                key="venus",
                label="Венера",
                longitude=18,
                sign="Овен",
                degree_in_sign=18,
            ),
            PlanetPosition(
                key="mars",
                label="Марс",
                longitude=185,
                sign="Весы",
                degree_in_sign=5,
            ),
        ],
        aspects=[],
    )

    sections = _fallback_sections(chart)
    ids = {section.id for section in sections}
    text = "\n".join(section.body_markdown for section in sections).lower()

    assert "section-work-money" in ids
    assert "section-shadow-patterns" in ids
    assert "section-relationships" in ids
    assert "например" in text
    assert "тен" in text
    assert "без точного времени" in text


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


@pytest.mark.asyncio
async def test_generate_interpretation_repairs_incomplete_practical_structure(monkeypatch):
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
            )
        ],
        aspects=[],
    )

    complete_response = (
        "## section-summary | Краткое резюме\n"
        "Есть общий вектор и живой пример.\n\n"
        "## section-identity | Ядро личности и способ проявляться\n"
        "Солнце в Водолее проявляется через независимость. Например, человек легче включается в проект, "
        "когда понимает его смысл. Теневая сторона — спорить с правилами только ради свободы.\n\n"
        "## section-emotions | Эмоциональные потребности и восстановление\n"
        "Например, восстановление требует честного ритма. Теневая сторона — игнорировать усталость.\n\n"
        "## section-thinking | Мышление, речь и решения\n"
        "Например, мысль быстрее собирается через систему. Теневая сторона — спорить вместо уточнения.\n\n"
        "## section-love | Любовь, симпатия и личные ценности\n"
        "Например, симпатия растет через уважение свободы. Теневая сторона — холодность.\n\n"
        "## section-action | Действие, конфликт и энергия\n"
        "Например, энергия включается через ясную цель. Теневая сторона — резкость.\n\n"
        "## section-work-money | Работа, деньги и реализация\n"
        "Например, деньги держатся там, где есть роль и критерий результата. Теневая сторона — распыление.\n\n"
        "## section-shadow-patterns | Тени и повторяющиеся сценарии\n"
        "Например, защита может выглядеть как независимость любой ценой. Теневая сторона становится видимой.\n\n"
        "## section-relationships | Отношения и близость\n"
        "Например, близость крепче, когда потребности названы словами. Теневая сторона — проверять молчанием.\n\n"
        "## section-growth | Вектор роста и практичные шаги\n"
        "Например, один честный разговор полезнее идеального плана. Теневая сторона — откладывать."
    )

    class FakeRouter:
        def __init__(self):
            self.calls = 0

        async def get_response(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return (
                    "## section-summary | Краткое резюме\n"
                    "Сухой ответ.\n\n"
                    "## section-sun | Солнце — ядро личности\n"
                    "Солнце в Водолее.",
                    0,
                )
            assert "улучши структуру" in kwargs["history"][0]["parts"][0].lower()
            return complete_response, 0

    router = FakeRouter()
    monkeypatch.setattr("app.config.settings", SimpleNamespace(RESEARCH_MODEL="test-model", DEFAULT_MODEL=""))
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)

    sections = await generate_interpretation(chart, user_id=123, chat_id=456)

    ids = {section.id for section in sections}
    assert router.calls == 2
    assert "section-work-money" in ids
    assert "section-shadow-patterns" in ids
    assert "section-relationships" in ids
    assert "Сухой ответ" not in "\n".join(section.body_markdown for section in sections)


@pytest.mark.asyncio
async def test_generate_interpretation_does_not_repair_complete_practical_structure(monkeypatch):
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
            )
        ],
        aspects=[],
    )
    response = (
        "## section-summary | Краткое резюме\n"
        "Например, карта показывает живой вектор. Теневая сторона тоже названа.\n\n"
        "## section-identity | Ядро личности и способ проявляться\n"
        "Солнце в Водолее. Например, человек ищет смысл. Теневая сторона — спор ради свободы.\n\n"
        "## section-emotions | Эмоциональные потребности и восстановление\n"
        "Например, нужен ритм. Теневая сторона — игнорировать усталость.\n\n"
        "## section-thinking | Мышление, речь и решения\n"
        "Например, помогает структура. Теневая сторона — застревать в спорах.\n\n"
        "## section-love | Любовь, симпатия и личные ценности\n"
        "Например, важна честность. Теневая сторона — холодность.\n\n"
        "## section-action | Действие, конфликт и энергия\n"
        "Например, нужна цель. Теневая сторона — резкость.\n\n"
        "## section-work-money | Работа, деньги и реализация\n"
        "Например, результат держится на роли. Теневая сторона — распыление.\n\n"
        "## section-shadow-patterns | Тени и повторяющиеся сценарии\n"
        "Например, защита может стать привычкой. Теневая сторона видна в повторе.\n\n"
        "## section-relationships | Отношения и близость\n"
        "Например, важен прямой разговор. Теневая сторона — молчаливые проверки.\n\n"
        "## section-growth | Вектор роста и практичные шаги\n"
        "Например, помогает один шаг. Теневая сторона — ждать идеала."
    )

    class FakeRouter:
        def __init__(self):
            self.calls = 0

        async def get_response(self, **kwargs):
            del kwargs
            self.calls += 1
            return response, 0

    router = FakeRouter()
    monkeypatch.setattr("app.config.settings", SimpleNamespace(RESEARCH_MODEL="test-model", DEFAULT_MODEL=""))
    monkeypatch.setattr("app.providers.get_provider_router", lambda: router)

    sections = await generate_interpretation(chart, user_id=123, chat_id=456)

    assert router.calls == 1
    assert {section.id for section in sections} >= {"section-work-money", "section-shadow-patterns", "section-relationships"}
