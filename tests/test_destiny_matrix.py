from app.natal.destiny_matrix import (
    build_destiny_matrix_sections,
    calculate_destiny_matrix,
    render_destiny_matrix_svg,
)


def test_calculate_destiny_matrix_uses_stable_22_arcana_positions():
    matrix = calculate_destiny_matrix("1997-11-09")
    by_key = {position.key: position for position in matrix.positions}

    assert matrix.system == "destiny-matrix-22"
    assert by_key["day"].arcana == 9
    assert by_key["month"].arcana == 11
    assert by_key["year"].arcana == 4
    assert by_key["center"].arcana == 2
    assert by_key["relationship"].arcana == 11
    assert by_key["money"].arcana == 13
    assert by_key["mission"].arcana == 4
    assert by_key["day"].arcana_label == "Отшельник"
    assert by_key["center"].arcana_label == "Верховная Жрица"


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
    assert "Верховная Жрица" in svg
    assert "<script" not in svg.lower()
    assert "javascript:" not in svg.lower()


def test_build_destiny_matrix_sections_returns_user_facing_interpretation():
    matrix = calculate_destiny_matrix("1997-11-09")

    sections = build_destiny_matrix_sections(matrix)

    assert [section.id for section in sections][:3] == [
        "section-destiny-matrix",
        "section-destiny-relationship",
        "section-destiny-money",
    ]
    assert "фат" not in "\n".join(section.body_markdown.lower() for section in sections)
    assert any("саморефлекс" in section.body_markdown.lower() for section in sections)
