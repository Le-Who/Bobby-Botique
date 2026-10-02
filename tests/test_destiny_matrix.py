import xml.etree.ElementTree as ET

import pytest

from app.natal.destiny_matrix import (
    build_destiny_matrix_sections,
    calculate_destiny_matrix,
    render_destiny_matrix_svg,
)


def test_calculate_destiny_matrix_uses_stable_22_arcana_positions():
    matrix = calculate_destiny_matrix("2003-06-30")
    by_key = {position.key: position for position in matrix.positions}
    line_keys = {line.key for line in matrix.lines}

    assert matrix.system == "destiny-matrix-22"
    assert by_key["portrait"].arcana == 3
    assert by_key["higher_self"].arcana == 6
    assert by_key["soul_task"].arcana == 5
    assert by_key["comfort"].arcana == 14
    assert by_key["center"].arcana == 10
    assert by_key["male_talent"].arcana == 9
    assert by_key["female_talent"].arcana == 11
    assert by_key["money_channel"].arcana == 19
    assert by_key["karmic_tail"].arcana == 17
    assert by_key["portrait"].arcana_label == "Императрица"
    assert by_key["center"].arcana_label == "Колесо Фортуны"
    assert {"love_line", "money_line", "male_line", "female_line", "karmic_tail"} <= line_keys
    assert [period.start_age for period in matrix.life_periods] == [0, 10, 20, 30, 40, 50, 60, 70]


def test_calculate_destiny_matrix_includes_intermediate_ray_values():
    matrix = calculate_destiny_matrix("2003-06-30")
    by_key = {position.key: position for position in matrix.positions}

    assert by_key["axis_left_outer"].arcana == 16
    assert by_key["axis_left_mid"].arcana == 13
    assert by_key["axis_left_inner"].arcana == 5
    assert by_key["axis_top_outer"].arcana == 22
    assert by_key["axis_top_mid"].arcana == 16
    assert by_key["axis_top_inner"].arcana == 8
    assert by_key["axis_bottom_outer"].arcana == 20
    assert by_key["axis_bottom_mid"].arcana == 6
    assert any(position.kind == "intermediate" for position in matrix.positions)


@pytest.mark.parametrize(
    ("line_key", "expected_positions", "expected_summary"),
    [
        (
            "male_line",
            ["male_talent", "center", "money_channel"],
            "9. Отшельник → 10. Колесо Фортуны → 19. Солнце",
        ),
        (
            "female_line",
            ["female_talent", "center", "karmic_tail"],
            "11. Справедливость → 10. Колесо Фортуны → 17. Звезда",
        ),
    ],
)
def test_lineage_data_follows_the_drawn_diagonals_through_center(line_key, expected_positions, expected_summary):
    matrix = calculate_destiny_matrix("2003-06-30")
    line = next(line for line in matrix.lines if line.key == line_key)

    assert line.position_keys == expected_positions
    assert line.summary == expected_summary


def test_render_destiny_matrix_svg_follows_line_positions_from_data():
    matrix = calculate_destiny_matrix("2003-06-30")
    male_line = next(line for line in matrix.lines if line.key == "male_line")
    male_line.position_keys = ["portrait", "center", "soul_task"]
    male_line.summary = "3. Императрица → 10. Колесо Фортуны → 5. Иерофант"
    matrix.lines = [male_line]

    root = ET.fromstring(render_destiny_matrix_svg(matrix))
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    rendered_line = root.find("svg:g[@data-line='male_line']", namespace)

    assert rendered_line is not None
    assert rendered_line.find("svg:title", namespace).text == (
        "Мужская родовая линия: 3. Императрица → 10. Колесо Фортуны → 5. Иерофант"
    )
    segments = [
        tuple(float(segment.attrib[name]) for name in ("x1", "y1", "x2", "y2"))
        for segment in rendered_line.findall("svg:line", namespace)
    ]
    assert segments == [(130.0, 460.0, 460.0, 460.0), (460.0, 460.0, 790.0, 460.0)]


def test_comfort_label_matches_the_position_interpreted_as_comfort():
    matrix = calculate_destiny_matrix("2003-06-30")
    comfort_positions = [position for position in matrix.positions if "зона комфорта" in position.label.lower()]
    sections = build_destiny_matrix_sections(matrix)
    comfort_section = next(section for section in sections if section.id == "section-destiny-comfort")

    assert [position.key for position in comfort_positions] == ["comfort"]
    assert comfort_positions[0].arcana == 14
    assert "Зона комфорта — **14. Умеренность**" in comfort_section.body_markdown
    assert "destiny:comfort" in comfort_section.chart_refs


def test_lineage_interpretation_names_all_positions_in_each_diagonal():
    matrix = calculate_destiny_matrix("2003-06-30")
    sections = build_destiny_matrix_sections(matrix)
    lineage = next(section for section in sections if section.id == "section-destiny-lineage")

    assert "Мужская линия соединяет **9. Отшельник**, **10. Колесо Фортуны** и **19. Солнце**" in lineage.body_markdown
    assert (
        "Женская линия соединяет **11. Справедливость**, **10. Колесо Фортуны** и **17. Звезда**"
        in lineage.body_markdown
    )
    assert {"destiny:male_talent", "destiny:center", "destiny:money_channel"} <= set(lineage.chart_refs)
    assert {"destiny:female_talent", "destiny:center", "destiny:karmic_tail"} <= set(lineage.chart_refs)


@pytest.mark.parametrize("birth_date", ["20030630", "2003-W27-1"])
def test_calculate_destiny_matrix_rejects_iso_dates_outside_yyyy_mm_dd_format(birth_date):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        calculate_destiny_matrix(birth_date)


def test_calculate_destiny_matrix_rejects_invalid_birth_date():
    try:
        calculate_destiny_matrix("1997-99-99")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("invalid date must raise ValueError")


def test_render_destiny_matrix_svg_is_accessible_and_sanitized():
    matrix = calculate_destiny_matrix("2003-06-30")

    svg = render_destiny_matrix_svg(matrix)

    assert svg.startswith("<svg")
    assert 'role="img"' in svg
    assert "Матрица судьбы" in svg
    assert 'data-position="center"' in svg
    assert 'data-position="axis_left_mid"' in svg
    assert "Колесо Фортуны" in svg
    for age in range(0, 80, 10):
        assert f"{age} лет" in svg
    assert "линия отношений" in svg
    assert "денежный канал" in svg
    assert "мужской род" in svg
    assert "женский род" in svg
    assert "кармический хвост" in svg
    assert "Архетипы помогают смотреть" not in svg
    assert "<script" not in svg.lower()
    assert "javascript:" not in svg.lower()


def test_build_destiny_matrix_sections_returns_user_facing_interpretation():
    matrix = calculate_destiny_matrix("1997-11-09")

    sections = build_destiny_matrix_sections(matrix)
    section_ids = [section.id for section in sections]

    assert section_ids[:3] == [
        "section-destiny-matrix",
        "section-destiny-comfort",
        "section-destiny-relationships",
    ]
    assert "section-destiny-money" in section_ids
    assert "section-destiny-self-search" in section_ids
    assert "section-destiny-socialization" in section_ids
    assert "section-destiny-spiritual" in section_ids
    assert "section-destiny-energy" in section_ids
    assert len(sections) >= 9
    full_text = "\n".join(section.body_markdown for section in sections)
    lowered = full_text.lower()
    assert "Ваша центральная энергия — **11. Справедливость**" in full_text
    assert "портрет — **9. Отшельник**" in full_text
    assert "денежный канал — **18. Луна**" in full_text
    assert "Поиск себя" in full_text
    assert "Социализация" in full_text
    assert "Духовная гармония" in full_text
    assert "энергетический ритм" in lowered
    assert "### 0-9 лет — 9. Отшельник" in full_text
    assert "**Главный сюжет.**" in full_text
    assert "**Как это может проявиться.**" in full_text
    assert "**Теневой риск.**" in full_text
    assert "**Практичный ориентир.**" in full_text
    assert "Возможные события периода" not in full_text
    assert "0-9 лет" in full_text
    assert "10-19 лет" in full_text
    assert "смен" in lowered
    assert "например" in lowered
    assert "тен" in lowered
    assert "когда" in lowered
    assert "в вашем случае" in lowered
    assert "что в этой позиции должно быть" not in lowered
    assert "не прогноз" not in lowered
    assert "не заменяет" not in lowered
    assert "фат" not in lowered


def test_destiny_matrix_sections_speak_directly_without_distant_person_language():
    matrix = calculate_destiny_matrix("2003-06-30")

    sections = build_destiny_matrix_sections(matrix)
    lowered = "\n".join(section.body_markdown for section in sections).lower()

    assert "вы можете" in lowered
    assert "человек либо" not in lowered
    assert "человек может" not in lowered
