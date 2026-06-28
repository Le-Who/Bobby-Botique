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
    assert "Возможные события периода" in full_text
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
