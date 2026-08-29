from __future__ import annotations

import logging
import re

from app.config import GEMINI_PRIMARY_MODEL
from app.natal.models import ChartData, ReportSection
from app.natal.text_safety import (
    contains_user_facing_blocked_language,
    sanitize_user_facing_sections,
)

logger = logging.getLogger(__name__)

_POINT_TITLE_HINTS = {
    "sun": "Солнце — ядро личности",
    "moon": "Луна — эмоции и потребности",
    "mercury": "Меркурий — мышление и речь",
    "venus": "Венера — любовь и ценности",
    "mars": "Марс — энергия и действие",
    "jupiter": "Юпитер — рост и возможности",
    "saturn": "Сатурн — границы и ответственность",
    "uranus": "Уран — свобода и перемены",
    "neptune": "Нептун — интуиция и мечты",
    "pluto": "Плутон — глубина и трансформация",
}

_PRACTICAL_SECTION_HINTS: tuple[tuple[str, str], ...] = (
    ("section-identity", "Ядро личности и способ проявляться"),
    ("section-emotions", "Эмоциональные потребности и восстановление"),
    ("section-thinking", "Мышление, речь и решения"),
    ("section-love", "Любовь, симпатия и личные ценности"),
    ("section-action", "Действие, конфликт и энергия"),
    ("section-work-money", "Работа, деньги и реализация"),
    ("section-shadow-patterns", "Тени и повторяющиеся сценарии"),
    ("section-relationships", "Отношения и близость"),
    ("section-growth", "Вектор роста и практичные шаги"),
)

_SIGN_ALIASES = {
    "Овен": ("овен", "овне", "овна"),
    "Телец": ("телец", "тельце", "тельца"),
    "Близнецы": ("близнецы", "близнецах", "близнецов"),
    "Рак": ("рак", "раке", "рака"),
    "Лев": ("лев", "льве", "льва"),
    "Дева": ("дева", "деве", "девы"),
    "Весы": ("весы", "весах", "весов"),
    "Скорпион": ("скорпион", "скорпионе", "скорпиона"),
    "Стрелец": ("стрелец", "стрельце", "стрельца"),
    "Козерог": ("козерог", "козероге", "козерога"),
    "Водолей": ("водолей", "водолее", "водолея"),
    "Рыбы": ("рыбы", "рыбах", "рыб"),
}

_NATAL_LLM_TIMEOUT_SECONDS = 45
_NATAL_LLM_MAX_KEY_RETRIES = 2
_NATAL_INTERPRETATION_MODEL = GEMINI_PRIMARY_MODEL
_ABSTRACT_STYLE_RE = re.compile(
    "|".join(
        (
            r"астрологическ\w*\s+сетка",
            r"проецир\w*\s+на\s+сфер",
            r"\bнатив\b",
            r"сфер[ау]\s+ваших\s+личных\s+ресурсов",
        )
    ),
    re.IGNORECASE,
)


def build_interpretation_prompt(chart: ChartData, language: str, focus: str) -> str:
    section_ids = [
        "section-summary",
        *(section_id for section_id, _title in _PRACTICAL_SECTION_HINTS),
        "section-aspects",
    ]
    title_guidance = "\n".join(f"- {hint}" for hint in _title_hints_for_chart(chart))
    confidence_rule = ""
    if not chart.input_quality.houses_available:
        confidence_rule = "Время неизвестно: не трактуй дома, Асцендент или MC как достоверные факты."
    quality_block = _prompt_quality_constraints(chart)
    return (
        "Ты пишешь текстовую интерпретацию натальной карты по уже рассчитанным данным.\n"
        "Не запрашивай и не восстанавливай сырые дату рождения, место рождения или личные данные.\n"
        f"Язык ответа: {language or 'ru'}.\n"
        f"Фокус: {focus or 'general'}.\n"
        f"{confidence_rule}\n"
        f"{quality_block}\n"
        "Запрещены фаталистичные формулировки, медицинская, финансовая или юридическая определенность.\n"
        "Верни markdown-секции. Каждая секция должна начинаться заголовком вида `## section-id | Заголовок`.\n"
        "Структура должна ощущаться как практичные темы, которые человек может открывать по интересу.\n"
        "Обращайся к человеку напрямую, на «вы»: не как к объекту анализа, а как к живому человеку с выбором, "
        "сомнениями, привычками и взрослыми решениями.\n"
        "Тон должен не звучать как справочник, анкета или механический список признаков; это живой разбор "
        "конкретного человека, где расчетные точки объясняют опыт, а не заменяют его.\n"
        "Не перечисляй расчетные точки подряд: связывай их в цельный человеческий сюжет, где видно, как одна тема "
        "поддерживает или осложняет другую.\n"
        "Пиши как спокойный практик: с теплом, наблюдательностью и легкой образностью, но без театральной мистики, "
        "эмодзи и пророческого пафоса.\n"
        "Иногда можно использовать мягкие формулировки гипотезы вроде «похоже» или «в вашем случае это может "
        "звучать как», но не превращай каждую секцию в набор оговорок.\n"
        "Не пиши канцелярско-академические обороты вроде «астрологическая сетка указывает», "
        "«проецируется на сферу», «натив»: переводи расчетные идеи в простой человеческий опыт.\n"
        "В каждой крупной теме: сначала смысл, затем как это проявляется в жизни, затем теневой риск и один "
        "понятный пример. Используй жизненные примеры, когда они помогают узнать ситуацию в себе.\n"
        "Пиши честно: можно освещать негативные стороны, но без приговора, без лести и без запугивания.\n"
        "не упоминай оплату, тарифы, личный кабинет или форму ввода; не пиши, что разбор бесплатный.\n"
        "Не создавай отдельные секции по каждой планете, если та же тема уже есть в практичном блоке. "
        "Планеты используй внутри жизненных тем как расчетную опору.\n"
        "Заголовки делай короткими и смысловыми: сначала тема жизни, а планету и образ переноси в текст блока.\n"
        f"Ориентиры для заголовков:\n{title_guidance}\n"
        f"Обязательные stable ids: {', '.join(section_ids)}.\n"
        "Для русского языка пиши кратко, глубоко и бережно.\n"
        "ChartData JSON:\n"
        f"{_chart_prompt_json(chart)}"
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
        from app.providers import get_provider_router

        router = get_provider_router()
        response, _tokens = await router.get_response(
            preferred_model=_NATAL_INTERPRETATION_MODEL,
            history=[{"role": "user", "parts": [prompt]}],
            user_id=user_id,
            chat_id=chat_id,
            max_key_retries=_NATAL_LLM_MAX_KEY_RETRIES,
            timeout=_NATAL_LLM_TIMEOUT_SECONDS,
        )
        sections = _parse_sections(response or "")
        if sections and _sections_contradict_calculated_signs(chart, sections):
            logger.warning(
                "Natal LLM interpretation rejected: response contradicted calculated planet signs",
            )
            return _fallback_sections(chart)
        if sections and _sections_need_quality_repair(sections):
            repair_prompt = _build_interpretation_repair_prompt(chart, language, focus, response or "")
            repaired_response, _repair_tokens = await router.get_response(
                preferred_model=_NATAL_INTERPRETATION_MODEL,
                history=[{"role": "user", "parts": [repair_prompt]}],
                user_id=user_id,
                chat_id=chat_id,
                max_key_retries=_NATAL_LLM_MAX_KEY_RETRIES,
                timeout=_NATAL_LLM_TIMEOUT_SECONDS,
            )
            repaired_sections = _parse_sections(repaired_response or "")
            if repaired_sections and not _sections_contradict_calculated_signs(chart, repaired_sections):
                return sanitize_user_facing_sections(repaired_sections) or _fallback_sections(chart)
            logger.warning(
                "Natal LLM repair rejected or unparsable: sections=%d response_len=%d",
                len(repaired_sections),
                len(repaired_response or ""),
            )
        if not sections:
            logger.warning(
                "Natal LLM interpretation was unparsable: response_len=%d",
                len(response or ""),
            )
        return sanitize_user_facing_sections(sections) or _fallback_sections(chart)
    except Exception as exc:
        logger.warning("Natal LLM interpretation failed: %s", exc, exc_info=True)
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


def _sections_need_quality_repair(sections: list[ReportSection]) -> bool:
    ids = {section.id for section in sections}
    required_ids = {"section-summary", *(section_id for section_id, _title in _PRACTICAL_SECTION_HINTS)}
    if not required_ids <= ids:
        return True

    joined = "\n".join(f"{section.title}\n{section.body_markdown}" for section in sections).lower()
    if contains_user_facing_blocked_language(joined):
        return True
    if _ABSTRACT_STYLE_RE.search(joined):
        return True
    has_example = "например" in joined or "пример" in joined
    has_shadow = "тен" in joined or "слаб" in joined or "риск" in joined
    return not (has_example and has_shadow)


def _build_interpretation_repair_prompt(chart: ChartData, language: str, focus: str, first_response: str) -> str:
    required = ", ".join(["section-summary", *(section_id for section_id, _title in _PRACTICAL_SECTION_HINTS)])
    return (
        "Улучши структуру уже сгенерированной интерпретации натальной карты.\n"
        "Не пересчитывай карту и не добавляй сырые дату рождения, место рождения или личные данные.\n"
        f"Язык ответа: {language or 'ru'}.\n"
        f"Фокус: {focus or 'general'}.\n"
        f"Обязательные stable ids для практичных раскрываемых тем: {required}.\n"
        "Каждая секция должна начинаться `## section-id | Заголовок`.\n"
        "Обращайся к человеку напрямую, на «вы», и убери справочный, механический тон.\n"
        "Убери технические примечания, названия внутренних расчетных инструментов, инженерные детали домов и "
        "служебные статусы проверки данных.\n"
        "Пиши простым русским языком: как внимательный друг или наставник, без академических оборотов "
        "вроде «астрологическая сетка», «проецируется на сферу» или «натив».\n"
        "Не перечисляй расчетные точки подряд: связывай их в цельный человеческий сюжет. Пиши как спокойный практик "
        "с теплом и наблюдательностью, без театральной мистики, эмодзи и пророческого пафоса.\n"
        "В каждой крупной теме добавь: смысл, бытовое проявление, теневую сторону и понятный пример.\n"
        "Пиши честно, без лести, без фатализма, без оплаты, тарифов, личного кабинета и формы ввода.\n"
        "Сохрани рассчитанные знаки планет из ChartData JSON и не противоречь им.\n"
        "ChartData JSON:\n"
        f"{_chart_prompt_json(chart)}\n\n"
        "Первый ответ, который нужно переработать:\n"
        f"{first_response}"
    )


def _fallback_sections(chart: ChartData) -> list[ReportSection]:
    planet_lines = [f"- {planet.label}: {planet.sign} {planet.degree_in_sign:.1f}°" for planet in chart.planets]
    aspect_lines = [f"- {aspect.point_a} {aspect.aspect} {aspect.point_b}, орб {aspect.orb:.1f}°" for aspect in chart.aspects]
    unavailable = "Глубокая LLM-интерпретация временно недоступна, поэтому ниже приведен базовый разбор по расчетным точкам."
    sun = next((planet for planet in chart.planets if planet.key == "sun"), None)
    moon = next((planet for planet in chart.planets if planet.key == "moon"), None)
    mercury = next((planet for planet in chart.planets if planet.key == "mercury"), None)
    venus = next((planet for planet in chart.planets if planet.key == "venus"), None)
    mars = next((planet for planet in chart.planets if planet.key == "mars"), None)
    saturn = next((planet for planet in chart.planets if planet.key == "saturn"), None)
    sections = [
        ReportSection(
            id="section-summary",
            title="Краткое резюме",
            body_markdown=(
                f"{unavailable}\n\n"
                f"{_time_context_note(chart)} Ниже — не приговор, а карта акцентов: где проще опереться на себя, "
                "где могут включаться тени и какие темы стоит наблюдать в обычных ситуациях.\n\n"
                + "\n".join(planet_lines[:10])
            ),
        ),
    ]
    sections.extend(
        [
            ReportSection(
                id="section-identity",
                title="Ядро личности и способ проявляться",
                body_markdown=(
                    f"{_placement_sentence(sun, 'Солнце')} Это базовый способ собирать волю и ощущать, что вы живете "
                    "своей жизнью. Например, в споре или новом проекте солнечная тема показывает, где хочется быть "
                    "заметным, полезным или самостоятельным.\n\n"
                    "Теневая сторона появляется, когда человек начинает доказывать свою ценность через роль, а не через "
                    "живое действие. Тогда полезно спросить себя: я сейчас выбираю из интереса или из желания заслужить "
                    "право быть собой?"
                ),
                chart_refs=["sun"],
            ),
            ReportSection(
                id="section-emotions",
                title="Эмоциональные потребности и восстановление",
                body_markdown=(
                    f"{_placement_sentence(moon, 'Луна')} Луна показывает, что помогает нервной системе выдохнуть и "
                    "почувствовать, что внутри снова есть место для себя. Например, один человек восстанавливается через "
                    "тишину, другой — через теплый контакт и возможность быть услышанным.\n\n"
                    f"{_time_context_note(chart)} Тень этой темы — игнорировать потребности до момента, когда эмоции уже "
                    "говорят за человека резче, чем он сам хотел бы."
                ),
                chart_refs=["moon"],
            ),
            ReportSection(
                id="section-thinking",
                title="Мышление, речь и решения",
                body_markdown=(
                    f"{_placement_sentence(mercury, 'Меркурий')} Этот блок про то, как удобнее думать, объяснять, "
                    "учиться и договариваться. Например, в рабочем чате это проявляется в стиле вопросов: человек либо "
                    "сразу ищет структуру, либо сначала собирает впечатления и только потом делает вывод.\n\n"
                    "Тень Меркурия — путать ясность с контролем: говорить много, но не слышать, или наоборот молчать, "
                    "пока ситуация сама не станет напряженной."
                ),
                chart_refs=["mercury"],
            ),
            ReportSection(
                id="section-love",
                title="Любовь, симпатия и личные ценности",
                body_markdown=(
                    f"{_placement_sentence(venus, 'Венера')} Венера описывает не только романтику, но и вкус к жизни: "
                    "что кажется красивым, честным, приятным, достойным вложения сил. Например, выбор партнера, одежды, "
                    "подарка или формата отдыха часто идет через эту настройку.\n\n"
                    "Тень Венеры — соглашаться на меньшее из страха потерять контакт или, наоборот, обесценивать тепло, "
                    "если оно не выглядит идеально."
                ),
                chart_refs=["venus"],
            ),
            ReportSection(
                id="section-action",
                title="Действие, конфликт и энергия",
                body_markdown=(
                    f"{_placement_sentence(mars, 'Марс')} Марс показывает, как человек включает инициативу, злость, "
                    "соревновательность и способность защищать свое. Например, в конфликте он может идти прямо, "
                    "торговаться, отступать для перегруппировки или ждать точного момента.\n\n"
                    "Теневая сторона — копить раздражение, а потом действовать резче, чем требует ситуация. Здоровый "
                    "вариант этой темы — замечать импульс раньше и выбирать форму действия."
                ),
                chart_refs=["mars"],
            ),
            ReportSection(
                id="section-work-money",
                title="Работа, деньги и реализация",
                body_markdown=(
                    "Рабочая реализация в натальной карте читается через связку Солнца, Меркурия, Марса, Сатурна и домов, "
                    "если время рождения известно. "
                    f"{_time_context_note(chart)} Например, если человек регулярно берет задачи без ясных границ, деньги "
                    "могут приходить вместе с усталостью; если заранее определены роль, срок и критерий результата, "
                    "та же энергия становится профессиональной опорой.\n\n"
                    f"{_placement_sentence(saturn, 'Сатурн')} Тень денежной темы — ждать идеального разрешения извне "
                    "или соглашаться на формат, где много ответственности и мало права влиять на правила."
                ),
                chart_refs=["sun", "mercury", "mars", "saturn"],
            ),
            ReportSection(
                id="section-shadow-patterns",
                title="Тени и повторяющиеся сценарии",
                body_markdown=(
                    "Тени карты — это не плохие качества, а способы защиты, которые когда-то помогали, но теперь могут "
                    "сужать выбор. Например, сильная потребность в независимости может превращаться в привычку не просить "
                    "помощи, даже когда поддержка ускорила бы результат.\n\n"
                    "Полезный вопрос для наблюдения: где я сейчас действую из живого желания, а где из страха быть "
                    "непонятым, отвергнутым или недостаточно сильным?"
                ),
            ),
            ReportSection(
                id="section-relationships",
                title="Отношения и близость",
                body_markdown=(
                    "Отношения собираются из Луны, Венеры, Марса и аспектов между личными планетами. Например, одному "
                    "человеку важно быстро проговаривать симпатию, другому — сначала увидеть устойчивость поступков.\n\n"
                    "Тень близости — ожидать, что партнер угадает внутренний сценарий без слов. Чем честнее названы "
                    "потребности, тем меньше приходится проверять любовь через дистанцию, ревность или молчание."
                ),
                chart_refs=["moon", "venus", "mars"],
            ),
            ReportSection(
                id="section-growth",
                title="Вектор роста и практичные шаги",
                body_markdown=(
                    "Практичный рост начинается не с попытки стать другим человеком, а с точной настройки уже видимых "
                    "качеств. Выберите одну тему из разбора и неделю наблюдайте ее в быту: как вы отвечаете на просьбы, "
                    "где соглашаетесь слишком быстро, где откладываете важный разговор.\n\n"
                    "Хороший маркер движения — не идеальность, а чуть больше выбора в привычной ситуации."
                ),
            ),
        ]
    )
    sections.append(
        ReportSection(
            id="section-aspects",
            title="Аспекты",
            body_markdown=f"{unavailable}\n\n" + ("\n".join(aspect_lines) if aspect_lines else "Мажорные аспекты не выделены."),
        )
    )
    return sections


def _planet_fact(planet, unavailable: str) -> str:
    if planet is None:
        return unavailable
    return f"{unavailable}\n\n{planet.label}: {planet.sign} {planet.degree_in_sign:.1f}°."


def _title_hints_for_chart(chart: ChartData) -> list[str]:
    hints = ["Краткое резюме — главные темы карты"]
    hints.extend(f"{title} — practical section id `{section_id}`" for section_id, title in _PRACTICAL_SECTION_HINTS)
    for planet in chart.planets:
        hints.append(f"Расчетная опора внутри тем: {planet.label} — {_point_meaning_for_prompt(planet.key)}")
    hints.append("Аспекты — внутренние связи и напряжения")
    if chart.houses or chart.input_quality.houses_available:
        hints.append("Дома — сферы жизни")
    return hints


def _point_title(point_key: str, fallback_label: str) -> str:
    return _POINT_TITLE_HINTS.get(point_key.lower(), fallback_label)


def _point_meaning_for_prompt(point_key: str) -> str:
    title = _POINT_TITLE_HINTS.get(point_key.lower())
    if not title or "—" not in title:
        return point_key
    return title.split("—", 1)[1].strip()


def _placement_sentence(planet, label: str) -> str:
    if planet is None:
        return f"{label} не выделено в расчетных данных fallback-версии."
    return f"{planet.label} в знаке {planet.sign} ({planet.degree_in_sign:.1f}°)."


def _time_context_note(chart: ChartData) -> str:
    if chart.input_quality.houses_available:
        return "При известном времени рождения сферы карты можно читать подробнее, но все равно мягко."
    return "Без точного времени рождения часть сфер карты лучше читать как ориентиры, а не как точные факты."


def _sections_contradict_calculated_signs(chart: ChartData, sections: list[ReportSection]) -> bool:
    if not chart.planets:
        return False
    markdown = "\n".join(f"{section.title}\n{section.body_markdown}" for section in sections).lower()
    planet_labels = [planet.label for planet in chart.planets if planet.label]
    for planet in chart.planets:
        wrong_aliases: list[str] = []
        for sign, aliases in _SIGN_ALIASES.items():
            if sign != planet.sign:
                wrong_aliases.extend(aliases)
        if _planet_mentions_any_sign_alias(markdown, planet.label, wrong_aliases, planet_labels):
            return True
    return False


def _planet_mentions_any_sign_alias(
    markdown: str,
    planet_label: str,
    sign_aliases: list[str],
    planet_labels: list[str] | None = None,
) -> bool:
    planet = re.escape(planet_label.lower())
    other_planets = [
        re.escape(label.lower())
        for label in (planet_labels or [])
        if label and label.lower() != planet_label.lower()
    ]
    for match in re.finditer(rf"\b{planet}\b", markdown):
        context = markdown[match.end() : match.end() + 180]
        cut_points: list[int] = []
        boundary = re.search(r"[.\n!?;:]", context)
        if boundary:
            cut_points.append(boundary.start())
        for other_planet in other_planets:
            other_match = re.search(rf"\b{other_planet}\b", context)
            if other_match:
                cut_points.append(other_match.start())
        if cut_points:
            context = context[: min(cut_points)]
        for alias in sign_aliases:
            sign = re.escape(alias)
            if re.search(rf"\b(?:в|во)\s+(?:знаке\s+)?{sign}\b", context):
                return True
    return False


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


def _chart_prompt_json(chart: ChartData) -> str:
    return _chart_for_prompt(chart).model_dump_json(
        exclude={"input_quality": {"calculation_engine", "reference_validated", "warnings"}}
    )


def _prompt_quality_constraints(chart: ChartData) -> str:
    lines = [
        "Если используешь дома или углы, подавай их мягко: как ориентиры, а не как полностью проверенные факты.",
        "Для пользователя не пиши технические примечания: не называй внутренние проверки, библиотеку расчета, "
        "служебные статусы или инженерную систему домов.",
    ]
    safe_notes = _safe_prompt_quality_notes(chart)
    if safe_notes:
        lines.append("Внутренние ограничения данных (не выводить пользователю):")
        lines.extend(f"- {note}" for note in safe_notes)
    return "\n".join(lines)


_BIRTH_DATA_RE = re.compile(r"\b(?:birth_date|birth_place)\b|дата рождения|место рождения", flags=re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_RU_DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")


def _safe_prompt_quality_notes(chart: ChartData) -> list[str]:
    notes: list[str] = []
    for warning in chart.input_quality.warnings:
        redacted = _redact_raw_birth_data(warning)
        if redacted and not contains_user_facing_blocked_language(redacted):
            notes.append(redacted)
    return notes


def _redact_raw_birth_data(value: str) -> str:
    if _BIRTH_DATA_RE.search(value):
        return "[redacted birth data]"
    redacted = _ISO_DATE_RE.sub("[redacted date]", value)
    return _RU_DATE_RE.sub("[redacted date]", redacted)
