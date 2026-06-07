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
    assert chart.input_quality.calculation_engine == "ephem-local"
    assert chart.input_quality.reference_validated is False
    assert not any("эврист" in warning.lower() for warning in chart.input_quality.warnings)


@pytest.mark.asyncio
async def test_angles_depend_on_latitude_and_mc_is_not_derived_from_ascendant():
    base = ResolvedBirthData(
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
    equator = base.model_copy(update={"latitude": 0.0})

    kyiv_chart = await calculate_chart(base)
    equator_chart = await calculate_chart(equator)

    assert abs(kyiv_chart.angles["ascendant"] - equator_chart.angles["ascendant"]) > 1.0
    toy_mc = (kyiv_chart.angles["ascendant"] + 270.0) % 360.0
    assert abs(kyiv_chart.angles["mc"] - toy_mc) > 1.0


@pytest.mark.asyncio
async def test_calculate_marks_retrograde_planets():
    resolved = ResolvedBirthData(
        birth_input=BirthInput(
            birth_date="2023-08-30",
            time_precision=TimePrecision.UNKNOWN,
            birth_place="Kyiv, Ukraine",
        ),
        latitude=50.4501,
        longitude=30.5234,
        timezone="Europe/Kyiv",
        local_datetime="2023-08-30T12:00:00+03:00",
        utc_datetime="2023-08-30T09:00:00+00:00",
        display_place="Kyiv, Ukraine",
    )

    chart = await calculate_chart(resolved)
    by_key = {planet.key: planet for planet in chart.planets}

    assert by_key["mercury"].retrograde is True
    assert by_key["sun"].retrograde is False
