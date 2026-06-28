from app.natal.destiny_matrix import (
    build_destiny_matrix_sections,
    calculate_destiny_matrix,
    render_destiny_matrix_svg,
)


def test_calculate_destiny_matrix_uses_stable_22_arcana_positions():
    matrix = calculate_destiny_matrix("1997-11-09")
    by_key = {position.key: position for position in matrix.positions}
    line_keys = {line.key for line in matrix.lines}

    assert matrix.system == "destiny-matrix-22"
    assert by_key["portrait"].arcana == 9
    assert by_key["higher_self"].arcana == 11
    assert by_key["soul_task"].arcana == 4
    assert by_key["comfort"].arcana == 2
    assert by_key["center"].arcana == 4
    assert by_key["portrait"].arcana_label == "Отшельник"
    assert by_key["center"].arcana_label == "Император"
    assert {"love_line", "money_line", "male_line", "female_line", "karmic_tail"} <= line_keys
    assert [period.start_age for period in matrix.life_periods] == [0, 10, 20, 30, 40, 50, 60, 70]


def test_calculate_destiny_matrix_rejects_invalid_birth_date():
    try:
        calculate_destiny_matrix("1997-99-99")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("invalid date must raise ValueError")


def test_render_destiny_matrix_svg_is_accessible_and_sanitized():
    matrix = calculate_destiny_matrix("1997-11-09")

    svg = render_destiny_matrix_svg(matrix)

    assert svg.startswith("<svg")
    assert 'role="img"' in svg
    assert "Матрица судьбы" in svg
    assert 'data-position="center"' in svg
    assert "Император" in svg
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

    assert [section.id for section in sections][:3] == [
        "section-destiny-matrix",
        "section-destiny-relationships",
        "section-destiny-money",
    ]
    full_text = "\n".join(section.body_markdown for section in sections)
    lowered = full_text.lower()
    assert "Ваша центральная энергия — **4. Император**" in full_text
    assert "портрет — **9. Отшельник**" in full_text
    assert "денежный канал — **6. Влюбленные**" in full_text
    assert "в вашем случае" in lowered
    assert "что в этой позиции должно быть" not in lowered
    assert "не прогноз" not in lowered
    assert "не заменяет" not in lowered
    assert "фат" not in lowered
