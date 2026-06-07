import pytest

from app.natal.calculator import calculate_chart
from app.natal.models import BirthInput, ResolvedBirthData, TimePrecision


def resolved_unknown_time() -> ResolvedBirthData:
    return ResolvedBirthData(
        birth_input=BirthInput(
            birth_date="1995-02-14",
            time_precision=TimePrecision.UNKNOWN,
            birth_place="Kyiv, Ukraine",
        ),
        latitude=50.4501,
        longitude=30.5234,
        timezone="Europe/Kyiv",
        local_datetime="1995-02-14T12:00:00+02:00",
        utc_datetime="1995-02-14T10:00:00+00:00",
        display_place="Kyiv, Ukraine",
    )


@pytest.mark.asyncio
async def test_calculate_unknown_time_disables_houses_and_angles():
    chart = await calculate_chart(resolved_unknown_time())

    assert chart.input_quality.houses_available is False
    assert chart.input_quality.angles_available is False
    assert chart.houses == []
    assert chart.angles == {}
    assert {planet.key for planet in chart.planets} >= {"sun", "moon", "mercury", "venus", "mars"}


@pytest.mark.asyncio
async def test_calculate_exact_time_includes_houses_and_angles():
    resolved = ResolvedBirthData(
        birth_input=BirthInput(
            birth_date="1995-02-14",
            time_precision=TimePrecision.EXACT,
            birth_time="06:30",
            birth_place="Kyiv, Ukraine",
        ),
        latitude=50.4501,
        longitude=30.5234,
        timezone="Europe/Kyiv",
        local_datetime="1995-02-14T06:30:00+02:00",
        utc_datetime="1995-02-14T04:30:00+00:00",
        display_place="Kyiv, Ukraine",
    )

    chart = await calculate_chart(resolved)

    assert chart.input_quality.houses_available is True
    assert len(chart.houses) == 12
    assert "ascendant" in chart.angles
    assert "mc" in chart.angles
