from app.natal.models import ChartData, InputQuality, PlanetPosition, TimePrecision
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
            )
        ],
        aspects=[],
    )


def test_render_svg_contains_accessible_anchors():
    svg = render_chart_svg(sample_chart())

    assert svg.startswith("<svg")
    assert 'href="#section-sun"' in svg
    assert "Солнце" in svg
    assert "Время рождения неизвестно" in svg


def test_render_svg_does_not_emit_script_tags():
    svg = render_chart_svg(sample_chart())

    assert "<script" not in svg.lower()
    assert "javascript:" not in svg.lower()
