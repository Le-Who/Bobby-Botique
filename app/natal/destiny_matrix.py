from __future__ import annotations

import html
import math
from datetime import date

from app.natal.models import (
    DestinyMatrixData,
    DestinyMatrixLifePeriod,
    DestinyMatrixLine,
    DestinyMatrixPosition,
    ReportSection,
)

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

_ARCANA_STRENGTHS: dict[int, str] = {
    1: "быстро запускать процессы, говорить прямо и собирать внимание вокруг идеи",
    2: "видеть скрытые мотивы, выдерживать паузу и считывать тонкие сигналы",
    3: "создавать атмосферу изобилия, заботиться о форме и выращивать результат постепенно",
    4: "выстраивать порядок, брать ответственность и превращать хаос в понятную систему",
    5: "объяснять сложное, держать ценности и быть проводником для других",
    6: "соединять людей, договариваться и выбирать через сердце без потери ясности",
    7: "вести к цели, выдерживать темп и переводить желание в действие",
    8: "влиять мягко, держать внутреннюю силу и не тратить ее на лишнюю борьбу",
    9: "углубляться в тему, работать автономно и находить собственную правду",
    10: "подхватывать удачные циклы, быстро перестраиваться и видеть окно возможностей",
    11: "сверять решения с честностью, держать баланс и оформлять ясные договоренности",
    12: "замечать новый ракурс, отпускать контроль и находить смысл в замедлении",
    13: "закрывать отжившее, обновлять правила и не держаться за старую роль",
    14: "настраивать ритм, примирять крайности и лечить систему через меру",
    15: "работать с сильными желаниями, деньгами, влиянием и личной магнетичностью",
    16: "быстро видеть слабые конструкции и строить заново после честной ревизии",
    17: "вдохновлять, держать дальний образ и возвращать надежду там, где ее не хватает",
    18: "работать с образами, эмоциями и неопределенностью без поспешных выводов",
    19: "проявляться ярко, согревать людей и делать результат заметным",
    20: "слышать зов большой задачи, работать с родовой темой и поднимать зрелые решения",
    21: "собирать разрозненное в целое, завершать циклы и видеть международный контекст",
    22: "начинать с чистого листа, пробовать новое и возвращать легкость в сложные процессы",
}

_ARCANA_GROWTH: dict[int, str] = {
    1: "не продавить все одной волей, а дать идее форму и партнеров",
    2: "не уходить в молчаливое ожидание, а проговаривать то, что уже понятно внутри",
    3: "не растворяться в заботе, а сохранять собственные желания и телесный ресурс",
    4: "не превращать порядок в жесткость, а оставлять место живому диалогу",
    5: "не спорить из позиции единственно верной истины, а проверять, что людям понятно",
    6: "не зависать в выборе, а формулировать честные критерии близости и сотрудничества",
    7: "не срываться в рывки, а удерживать маршрут, команду и восстановление",
    8: "не доказывать силу, а применять ее там, где она действительно меняет ситуацию",
    9: "не прятаться в одиночество, а выносить найденную глубину в контакт с людьми",
    10: "не ждать случайного поворота, а готовить систему к смене цикла",
    11: "не искать идеальную справедливость, а договариваться о рабочих правилах",
    12: "не зависать в жертвенности, а выбирать паузу как осознанный способ переоценки",
    13: "не разрушать ради свободы, а завершать так, чтобы осталось место новому",
    14: "не сглаживать все до беззубой середины, а беречь точную пропорцию",
    15: "не путать силу желания с зависимостью, а переводить страсть в созидание",
    16: "не рушить из раздражения, а разбирать конструкцию там, где она правда трещит",
    17: "не жить только будущим образом, а связывать вдохновение с ежедневными шагами",
    18: "не кормить тревогу фантазиями, а проверять чувства фактами и телом",
    19: "не выгорать от постоянной яркости, а делиться светом дозированно",
    20: "не тащить чужую родовую историю, а выбирать, какую часть опыта продолжать",
    21: "не распыляться на слишком широкий контекст, а доводить выбранный цикл до формы",
    22: "не бросать начатое при первом сопротивлении, а оставлять свободе опору",
}

_POSITION_LAYOUT: tuple[tuple[str, str, str, float, float, str], ...] = (
    ("higher_self", "Высшая суть", "вдохновение, связь с высшим, внутренний ориентир", 460, 130, "month"),
    ("female_talent", "Таланты женского рода", "поддержка, принятие, способы создавать отношения и среду", 693, 227, "derived"),
    ("soul_task", "Задача души", "социальная задача, зрелый выбор и направление усилий", 790, 460, "year"),
    ("money_channel", "Денежный канал", "вход в материальный результат, обмен и ценность", 693, 693, "derived"),
    ("comfort", "Характер и зона комфорта", "личная сила, привычный стиль восстановления и опоры", 460, 790, "derived"),
    ("karmic_tail", "Кармический хвост", "главный урок, повторяющийся сценарий и слабая зона", 227, 693, "derived"),
    ("portrait", "Портрет и ресурс", "визитная карточка, как считывают люди и мир", 130, 460, "day"),
    ("male_talent", "Таланты мужского рода", "воля, действие, стратегия и родовая линия отца", 227, 227, "derived"),
    ("center", "Центр личности", "главная сборка матрицы и зона личной силы", 460, 460, "derived"),
)

_PERIOD_KEYS: tuple[tuple[int, int, str, str], ...] = (
    (0, 9, "portrait", "ранняя среда, тело, первичный способ контакта с миром"),
    (10, 19, "male_talent", "подростковая воля, отношения с авторитетами, запуск самостоятельности"),
    (20, 29, "higher_self", "поиск смысла, вдохновения, учебы и внутреннего направления"),
    (30, 39, "female_talent", "отношения, среда, поддержка, раскрытие мягких навыков"),
    (40, 49, "soul_task", "социальная задача, зрелые решения и видимый вектор"),
    (50, 59, "money_channel", "материальная реализация, обмен, финансы и прикладная ценность"),
    (60, 69, "comfort", "переоценка характера, ритма, здоровья и личной опоры"),
    (70, 79, "karmic_tail", "сбор опыта, отпускание повторов и передача мудрости"),
)


def calculate_destiny_matrix(birth_date: str) -> DestinyMatrixData:
    parsed = _parse_birth_date(birth_date)
    portrait = _reduce_arcana(parsed.day)
    higher_self = _reduce_arcana(parsed.month)
    soul_task = _reduce_arcana(sum(int(char) for char in str(parsed.year)))
    comfort = _reduce_arcana(portrait + higher_self + soul_task)
    values = {
        "portrait": portrait,
        "higher_self": higher_self,
        "soul_task": soul_task,
        "comfort": comfort,
        "center": _reduce_arcana(portrait + higher_self + soul_task + comfort),
        "male_talent": _reduce_arcana(portrait + higher_self),
        "female_talent": _reduce_arcana(higher_self + soul_task),
        "money_channel": _reduce_arcana(soul_task + comfort),
        "karmic_tail": _reduce_arcana(portrait + comfort),
    }
    positions = [
        _build_position(key, label, theme, values[key], x, y)
        for key, label, theme, x, y, _source in _POSITION_LAYOUT
    ]
    by_key = {position.key: position for position in positions}
    return DestinyMatrixData(
        birth_date=parsed.isoformat(),
        positions=positions,
        lines=_build_lines(by_key),
        life_periods=_build_life_periods(by_key),
    )


def render_destiny_matrix_svg(matrix: DestinyMatrixData) -> str:
    by_key = {position.key: position for position in matrix.positions}
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 920 940" role="img" '
        'aria-labelledby="matrix-title matrix-desc">',
        '<title id="matrix-title">Матрица судьбы</title>',
        '<desc id="matrix-desc">Матрица судьбы по 22 арканам: центр, родовые линии, денежный канал, '
        'линия отношений и возрастные периоды.</desc>',
        "<defs>"
        '<radialGradient id="matrix-bg" cx="50%" cy="42%" r="72%">'
        '<stop offset="0%" stop-color="#fffef7"/><stop offset="58%" stop-color="#eef7f5"/>'
        '<stop offset="100%" stop-color="#e6eee9"/></radialGradient>'
        '<filter id="matrix-shadow" x="-30%" y="-30%" width="160%" height="160%">'
        '<feDropShadow dx="0" dy="10" stdDeviation="8" flood-color="#20352f" flood-opacity=".18"/>'
        "</filter>"
        "</defs>",
        '<rect width="920" height="940" rx="28" fill="url(#matrix-bg)"/>',
        '<circle cx="460" cy="460" r="360" fill="#fffefa" stroke="#1f332c" stroke-width="3"/>',
        '<circle cx="460" cy="460" r="292" fill="none" stroke="#93a19a" stroke-width="1.4" stroke-dasharray="5 9"/>',
        '<polygon points="460,130 693,227 790,460 693,693 460,790 227,693 130,460 227,227" '
        'fill="none" stroke="#1f332c" stroke-width="2"/>',
    ]
    parts.extend(_age_ticks())
    parts.extend(
        [
            _svg_line(by_key["portrait"], by_key["soul_task"], "#1f332c", 2.2, ".72"),
            _svg_line(by_key["higher_self"], by_key["comfort"], "#1f332c", 2.2, ".72"),
            _svg_line(by_key["male_talent"], by_key["money_channel"], "#2f69c9", 2.2, ".65"),
            _svg_line(by_key["female_talent"], by_key["karmic_tail"], "#d94d6a", 2.2, ".65"),
            _svg_line(by_key["karmic_tail"], by_key["center"], "#d94d6a", 2.6, ".64"),
            _svg_line(by_key["center"], by_key["female_talent"], "#d94d6a", 2.6, ".64"),
            _svg_line(by_key["comfort"], by_key["money_channel"], "#0f8a55", 2.6, ".70"),
            _svg_line(by_key["money_channel"], by_key["soul_task"], "#0f8a55", 2.6, ".70"),
        ]
    )
    parts.extend(
        [
            _line_label(314, 316, "мужской род", "#2f69c9"),
            _line_label(606, 316, "женский род", "#d94d6a"),
            _line_label(580, 590, "денежный канал", "#0f8a55"),
            _line_label(335, 585, "линия отношений", "#c02662"),
            _line_label(356, 736, "кармический хвост", "#7c3aed"),
            '<text x="548" y="634" text-anchor="middle" font-family="Aptos, Segoe UI, sans-serif" '
            'font-size="27" fill="#0f8a55">$</text>',
            '<text x="386" y="584" text-anchor="middle" font-family="Aptos, Segoe UI, sans-serif" '
            'font-size="26" fill="#c02662">♡</text>',
        ]
    )
    for position in matrix.positions:
        parts.append(_matrix_node(position))
    parts.extend(
        [
            _callout(460, 34, ["высшая суть", "вдохновение"], "#5b3fb0", "middle"),
            _callout(154, 126, ["таланты", "мужского рода"], "#2f69c9", "middle"),
            _callout(766, 126, ["таланты", "женского рода"], "#d94d6a", "middle"),
            _callout(858, 388, ["задача", "души"], "#9f331f", "middle"),
            _callout(812, 814, ["вход в", "денежный канал"], "#0f8a55", "middle"),
            _callout(460, 908, ["характер", "зона комфорта"], "#c05621", "middle"),
            _callout(108, 814, ["главный урок", "кармический хвост"], "#7c3aed", "middle"),
            _callout(62, 388, ["портрет", "личный ресурс"], "#0f766e", "middle"),
        ]
    )
    parts.append("</svg>")
    return "".join(parts)


def build_destiny_matrix_sections(matrix: DestinyMatrixData) -> list[ReportSection]:
    by_key = {position.key: position for position in matrix.positions}
    center = by_key["center"]
    portrait = by_key["portrait"]
    higher_self = by_key["higher_self"]
    soul_task = by_key["soul_task"]
    comfort = by_key["comfort"]
    male = by_key["male_talent"]
    female = by_key["female_talent"]
    money = by_key["money_channel"]
    karmic = by_key["karmic_tail"]
    return [
        ReportSection(
            id="section-destiny-matrix",
            title="Матрица судьбы: личный код",
            body_markdown=(
                f"Ваша центральная энергия — **{_arcana(center)}**. В вашем случае ядро матрицы лучше всего "
                f"читается через способность {center.interpretation}. Главная настройка роста — {center.shadow}.\n\n"
                f"Внешний портрет — **{_arcana(portrait)}**: люди быстрее всего считывают в вас способность "
                f"{portrait.interpretation}. Высшая суть — **{_arcana(higher_self)}** добавляет внутренний ориентир: "
                f"{higher_self.interpretation}. Задача души — **{_arcana(soul_task)}** переводит это в действие: "
                f"{soul_task.interpretation}.\n\n"
                f"Зона комфорта — **{_arcana(comfort)}** показывает, где легче восстанавливаться и возвращать себе опору: "
                f"{comfort.interpretation}."
            ),
            chart_refs=["destiny:center", "destiny:portrait", "destiny:higher_self", "destiny:soul_task"],
        ),
        ReportSection(
            id="section-destiny-relationships",
            title="Линия отношений и близости",
            body_markdown=(
                f"Линия отношений соединяет **{_arcana(karmic)}**, **{_arcana(center)}** и **{_arcana(female)}**. "
                f"В вашем случае близость раскрывается через честное признание повторяющегося урока "
                f"(**{karmic.arcana_label}**) и зрелую позицию центра (**{center.arcana_label}**). "
                f"Женская линия добавляет качество **{female.arcana_label}**: {female.interpretation}.\n\n"
                f"Практически это значит: отношения становятся сильнее, когда в фокусе задача — {center.shadow}, "
                "и меньше включается автоматическая защита или молчаливое ожидание."
            ),
            chart_refs=["destiny:karmic_tail", "destiny:center", "destiny:female_talent"],
        ),
        ReportSection(
            id="section-destiny-money",
            title="Денежный канал и реализация",
            body_markdown=(
                f"Ваш денежный канал — **{_arcana(money)}**. В вашем случае материальный результат легче приходит, "
                f"когда ценность создается через способность {money.interpretation}. "
                f"Связка с задачей души **{_arcana(soul_task)}** показывает, что деньги лучше держатся там, "
                f"где есть структура, понятная ответственность и видимый результат.\n\n"
                f"Слабая точка денежной линии — {money.shadow}. Если держать этот фокус, канал становится не абстрактной "
                "темой про удачу, а конкретным способом выбирать проекты, партнерства и формат работы."
            ),
            chart_refs=["destiny:money_channel", "destiny:soul_task", "destiny:comfort"],
        ),
        ReportSection(
            id="section-destiny-lineage",
            title="Родовые линии и кармический хвост",
            body_markdown=(
                f"Мужская линия — **{_arcana(male)}**: она показывает, как включаются воля, действие и отношение "
                f"к авторитетам; в вашем случае ресурс линии — {male.interpretation}. "
                f"Женская линия — **{_arcana(female)}**: здесь важны поддержка, принятие и умение создавать среду; "
                f"ее ресурс — {female.interpretation}.\n\n"
                f"Кармический хвост — **{_arcana(karmic)}**. Это повторяющийся урок матрицы: {karmic.theme}. "
                f"В вашем случае он просит {karmic.shadow}. Когда этот сценарий замечен, он становится источником "
                "зрелости, а не фоновым повтором."
            ),
            chart_refs=["destiny:male_talent", "destiny:female_talent", "destiny:karmic_tail"],
        ),
        ReportSection(
            id="section-destiny-periods",
            title="Возрастные периоды матрицы",
            body_markdown=_periods_markdown(matrix.life_periods),
            chart_refs=[f"destiny:age:{period.start_age}" for period in matrix.life_periods],
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


def _build_position(key: str, label: str, theme: str, arcana: int, x: float, y: float) -> DestinyMatrixPosition:
    return DestinyMatrixPosition(
        key=key,
        label=label,
        arcana=arcana,
        arcana_label=_ARCANA_LABELS[arcana],
        theme=theme,
        interpretation=_ARCANA_STRENGTHS[arcana],
        shadow=_ARCANA_GROWTH[arcana],
        x=x,
        y=y,
    )


def _build_lines(by_key: dict[str, DestinyMatrixPosition]) -> list[DestinyMatrixLine]:
    return [
        DestinyMatrixLine(
            key="love_line",
            label="Линия отношений",
            position_keys=["karmic_tail", "center", "female_talent"],
            summary=_line_summary("karmic_tail", "center", "female_talent", by_key),
        ),
        DestinyMatrixLine(
            key="money_line",
            label="Денежный канал",
            position_keys=["comfort", "money_channel", "soul_task"],
            summary=_line_summary("comfort", "money_channel", "soul_task", by_key),
        ),
        DestinyMatrixLine(
            key="male_line",
            label="Мужская родовая линия",
            position_keys=["portrait", "male_talent", "higher_self"],
            summary=_line_summary("portrait", "male_talent", "higher_self", by_key),
        ),
        DestinyMatrixLine(
            key="female_line",
            label="Женская родовая линия",
            position_keys=["higher_self", "female_talent", "soul_task"],
            summary=_line_summary("higher_self", "female_talent", "soul_task", by_key),
        ),
        DestinyMatrixLine(
            key="karmic_tail",
            label="Кармический хвост",
            position_keys=["portrait", "karmic_tail", "comfort"],
            summary=_line_summary("portrait", "karmic_tail", "comfort", by_key),
        ),
    ]


def _build_life_periods(by_key: dict[str, DestinyMatrixPosition]) -> list[DestinyMatrixLifePeriod]:
    periods: list[DestinyMatrixLifePeriod] = []
    for start_age, end_age, position_key, focus in _PERIOD_KEYS:
        position = by_key[position_key]
        periods.append(
            DestinyMatrixLifePeriod(
                start_age=start_age,
                end_age=end_age,
                arcana=position.arcana,
                arcana_label=position.arcana_label,
                theme=position.theme,
                focus=focus,
            )
        )
    return periods


def _line_summary(
    first_key: str, second_key: str, third_key: str, by_key: dict[str, DestinyMatrixPosition]
) -> str:
    first = by_key[first_key]
    second = by_key[second_key]
    third = by_key[third_key]
    return f"{_arcana(first)} → {_arcana(second)} → {_arcana(third)}"


def _age_ticks() -> list[str]:
    parts: list[str] = []
    for age in range(0, 80, 5):
        angle = math.radians(180 - age / 80 * 360)
        inner_x = 460 + math.cos(angle) * 354
        inner_y = 460 + math.sin(angle) * 354
        outer_x = 460 + math.cos(angle) * 366
        outer_y = 460 + math.sin(angle) * 366
        parts.append(
            f'<line x1="{inner_x:.1f}" y1="{inner_y:.1f}" x2="{outer_x:.1f}" y2="{outer_y:.1f}" '
            'stroke="#1f332c" stroke-width="2" stroke-linecap="round"/>'
        )
    age_labels = (
        (0, 52, 468),
        (10, 178, 170),
        (20, 460, 82),
        (30, 742, 170),
        (40, 876, 468),
        (50, 742, 766),
        (60, 460, 854),
        (70, 178, 766),
    )
    for age, x, y in age_labels:
        parts.append(
            f'<text x="{x}" y="{y}" text-anchor="middle" font-family="Aptos, Segoe UI, sans-serif" '
            f'font-size="20" font-weight="800" fill="#111827">{age} лет</text>'
        )
    return parts


def _svg_line(
    first: DestinyMatrixPosition, second: DestinyMatrixPosition, color: str, width: float, opacity: str
) -> str:
    return (
        f'<line x1="{first.x:.1f}" y1="{first.y:.1f}" x2="{second.x:.1f}" y2="{second.y:.1f}" '
        f'stroke="{color}" stroke-width="{width:.1f}" stroke-linecap="round" opacity="{opacity}"/>'
    )


def _line_label(x: float, y: float, label: str, color: str) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" font-family="Aptos, Segoe UI, sans-serif" '
        f'font-size="15" font-weight="800" fill="{color}">{html.escape(label)}</text>'
    )


def _matrix_node(position: DestinyMatrixPosition) -> str:
    radius = 55 if position.key == "center" else 45 if position.key in {
        "higher_self",
        "soul_task",
        "comfort",
        "portrait",
    } else 33
    fill_by_key = {
        "center": "#f8d84e",
        "higher_self": "#8d5bd6",
        "soul_task": "#d94d45",
        "comfort": "#ef7047",
        "portrait": "#7c55ca",
        "money_channel": "#f3a64d",
        "karmic_tail": "#f6f0e8",
        "male_talent": "#eff6ff",
        "female_talent": "#fff1f2",
    }
    fill = fill_by_key.get(position.key, "#fffefa")
    title = html.escape(f"{position.label}: {position.arcana}. {position.arcana_label} — {position.theme}")
    return (
        f'<g data-position="{html.escape(position.key, quote=True)}" filter="url(#matrix-shadow)">'
        f"<title>{title}</title>"
        f'<circle cx="{position.x:.1f}" cy="{position.y:.1f}" r="{radius}" fill="{fill}" '
        'stroke="#1f332c" stroke-width="2.2"/>'
        f'<text x="{position.x:.1f}" y="{position.y + 10:.1f}" text-anchor="middle" '
        'font-family="Georgia, serif" font-size="34" font-weight="700" fill="#16251f">'
        f"{position.arcana}</text>"
        "</g>"
    )


def _callout(x: float, y: float, lines: list[str], color: str, anchor: str) -> str:
    escaped = [html.escape(line) for line in lines]
    tspans = "".join(
        f'<tspan x="{x:.1f}" dy="{0 if index == 0 else 18}">{line}</tspan>'
        for index, line in enumerate(escaped)
    )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-family="Aptos, Segoe UI, sans-serif" '
        f'font-size="14" font-weight="800" fill="{color}">{tspans}</text>'
    )


def _periods_markdown(periods: list[DestinyMatrixLifePeriod]) -> str:
    lines = [
        "Возрастной контур матрицы читается как последовательность акцентов: какие качества чаще требуют внимания "
        "в разные десятилетия жизни.",
        "",
    ]
    for period in periods:
        lines.append(
            f"- **{period.start_age}-{period.end_age} лет — {period.arcana}. {period.arcana_label}**: "
            f"{period.focus}. Основной мотив периода — {period.theme}."
        )
    return "\n".join(lines)


def _arcana(position: DestinyMatrixPosition | DestinyMatrixLifePeriod) -> str:
    return f"{position.arcana}. {position.arcana_label}"
