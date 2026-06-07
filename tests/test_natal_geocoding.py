import pytest

from app.natal.geocoding import GeocodeResult, resolve_birth_data
from app.natal.models import BirthInput, TimePrecision


class FakeGeocoder:
    called = False

    async def geocode(self, place: str) -> GeocodeResult:
        self.called = True
        return GeocodeResult(
            display_name="Kyiv, Ukraine",
            latitude=50.4501,
            longitude=30.5234,
        )


@pytest.mark.asyncio
async def test_resolve_birth_data_unknown_time_uses_local_noon():
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    resolved = await resolve_birth_data(birth, geocoder=FakeGeocoder())

    assert resolved.timezone == "Europe/Kyiv"
    assert "12:00:00" in resolved.local_datetime
    assert resolved.latitude == 50.4501


@pytest.mark.asyncio
async def test_resolve_birth_data_uses_embedded_city_coordinates_without_network():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Odesa, Ukraine",
        birth_place_geoname_id="698740",
        birth_place_latitude=46.47747,
        birth_place_longitude=30.73262,
        birth_place_timezone="Europe/Kyiv",
        birth_place_display_name="Odesa, Ukraine",
    )

    resolved = await resolve_birth_data(birth, geocoder=geocoder)

    assert geocoder.called is False
    assert resolved.display_place == "Odesa, Ukraine"
    assert resolved.latitude == 46.47747
    assert resolved.longitude == 30.73262
    assert resolved.timezone == "Europe/Kyiv"
