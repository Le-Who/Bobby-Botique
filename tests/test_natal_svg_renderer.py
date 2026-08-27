from app.natal.models import Aspect, ChartData, House, InputQuality, PlanetPosition, TimePrecision
from app.natal.svg_renderer import render_chart_svg


def sample_chart() -> ChartData:
    return ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.UNKNOWN,
            houses_available=False,
            angles_available=False,
        ),
        planets=[
            PlanetPosition(
                key="sun",
                label="Солнце",
                longitude=325.0,
                sign="Водолей",
                degree_in_sign=25.0,
                house=3,
            ),
            PlanetPosition(
                key="moon",
                label="Луна",
                longitude=45.0,
                sign="Телец",
                degree_in_sign=15.0,
                house=6,
            ),
        ],
        aspects=[Aspect(point_a="sun", point_b="moon", aspect="square", orb=2.4)],
        houses=[House(number=1, cusp_longitude=10.0, sign="Овен")],
    )


def test_render_svg_contains_accessible_anchors():
    svg = render_chart_svg(sample_chart())

    assert svg.startswith("<svg")
    assert 'href="#section-sun"' in svg
    assert "Солнце" in svg
    assert "Время рождения неизвестно" in svg


def test_render_svg_uses_readable_zodiac_and_no_internal_text_legend():
    svg = render_chart_svg(sample_chart())

    assert "Овен" in svg
    assert "Рыбы" in svg
    assert 'class="chart-legend"' not in svg
    assert '<text x="44"' not in svg
    assert 'data-aspect="square"' in svg
    assert 'data-house="1"' in svg


def test_render_svg_uses_light_base_and_contrast_safe_chart_lines():
    svg = render_chart_svg(sample_chart())

    assert 'fill="#000' not in svg.lower()
    assert 'fill="black"' not in svg.lower()
    assert 'fill-opacity="0.92"' in svg
    assert 'stroke="#8a6ed0" stroke-width="3.2"' in svg
    assert 'stroke="#7d68a8" stroke-width="1.4"' in svg
    assert 'stroke-width="1.8" opacity="0.72"' in svg
    assert 'stroke="#6e5597" stroke-width="2.2"' in svg


def test_render_svg_does_not_emit_script_tags():
    svg = render_chart_svg(sample_chart())

    assert "<script" not in svg.lower()
    assert "javascript:" not in svg.lower()
