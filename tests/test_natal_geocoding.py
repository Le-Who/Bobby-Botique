import pytest

from app.natal.geocoding import GeocodeResult, resolve_birth_data
from app.natal.models import BirthInput, TimePrecision


class FakeGeocoder:
    async def geocode(self, place: str) -> GeocodeResult:
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
