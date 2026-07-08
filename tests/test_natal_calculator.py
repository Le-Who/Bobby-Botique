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


def resolved_unknown_time_for_date(birth_date: str, local_datetime: str, utc_datetime: str) -> ResolvedBirthData:
    return ResolvedBirthData(
        birth_input=BirthInput(
            birth_date=birth_date,
            time_precision=TimePrecision.UNKNOWN,
            birth_place="Kyiv, Ukraine",
        ),
        latitude=50.4501,
        longitude=30.5234,
        timezone="Europe/Kyiv",
        local_datetime=local_datetime,
        utc_datetime=utc_datetime,
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
async def test_unknown_time_marks_moon_uncertain_when_moon_sign_or_aspects_change():
    stable = resolved_unknown_time_for_date(
        "1982-10-05",
        "1982-10-05T12:00:00+02:00",
        "1982-10-05T10:00:00+00:00",
    )
    sign_change = resolved_unknown_time_for_date(
        "1995-02-01",
        "1995-02-01T12:00:00+02:00",
        "1995-02-01T10:00:00+00:00",
    )

    stable_chart = await calculate_chart(stable)
    sign_change_chart = await calculate_chart(sign_change)

    assert stable_chart.input_quality.moon_uncertainty is False
    assert sign_change_chart.input_quality.moon_uncertainty is True


@pytest.mark.asyncio
async def test_unknown_time_marks_moon_uncertain_when_moon_aspects_change():
    aspect_change = resolved_unknown_time_for_date(
        "1990-01-02",
        "1990-01-02T12:00:00+02:00",
        "1990-01-02T10:00:00+00:00",
    )

    chart = await calculate_chart(aspect_change)

    assert chart.input_quality.moon_uncertainty is True


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


@pytest.mark.asyncio
async def test_odesa_november_1997_keeps_sun_in_scorpio_not_virgo():
    resolved = ResolvedBirthData(
        birth_input=BirthInput(
            birth_date="1997-11-09",
            time_precision=TimePrecision.EXACT,
            birth_time="03:00",
            birth_place="Odesa, Ukraine",
            birth_place_country_code="UA",
        ),
        latitude=46.47747,
        longitude=30.73262,
        timezone="Europe/Kyiv",
        local_datetime="1997-11-09T03:00:00+02:00",
        utc_datetime="1997-11-09T01:00:00+00:00",
        display_place="Odesa, Odesa Oblast, Ukraine",
    )

    chart = await calculate_chart(resolved)
    by_key = {planet.key: planet for planet in chart.planets}

    assert by_key["sun"].sign == "Скорпион"
    assert by_key["sun"].degree_in_sign == pytest.approx(16.7, abs=0.1)
    assert by_key["moon"].sign == "Рыбы"
