import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.natal.geocoding import GeocodeResult, GeocodingError, resolve_birth_data
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


class ParisGeocoder:
    called = False

    async def geocode(self, place: str) -> GeocodeResult:
        self.called = True
        return GeocodeResult(
            display_name="Paris, France",
            latitude=48.8566,
            longitude=2.3522,
        )


class InvalidCoordinateGeocoder:
    called = False

    async def geocode(self, place: str) -> GeocodeResult:
        self.called = True
        return GeocodeResult(
            display_name="Invalid Coordinate City",
            latitude=48.8566,
            longitude=200.0,
        )


@pytest.mark.asyncio
async def test_resolve_birth_data_unknown_time_uses_local_noon():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Kyiv, Ukraine",
    )

    resolved = await resolve_birth_data(birth, geocoder=geocoder)

    assert geocoder.called is False
    assert resolved.timezone == "Europe/Kyiv"
    assert "12:00:00" in resolved.local_datetime
    assert 50.0 < resolved.latitude < 51.0
    assert 30.0 < resolved.longitude < 31.0


@pytest.mark.asyncio
async def test_resolve_birth_data_nominatim_fallback_resolves_timezone_from_coordinates():
    geocoder = ParisGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Definitely Missing Natal City",
    )

    resolved = await resolve_birth_data(birth, geocoder=geocoder, geocoder_provider="nominatim")

    assert geocoder.called is True
    assert resolved.timezone == "Europe/Paris"
    assert resolved.display_place == "Paris, France"


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_nonexistent_dst_local_time():
    birth = BirthInput(
        birth_date="2024-03-31",
        birth_time="03:30",
        time_precision=TimePrecision.EXACT,
        birth_place="Kyiv, Ukraine",
        birth_place_latitude=50.4501,
        birth_place_longitude=30.5234,
        birth_place_timezone="Europe/Kyiv",
    )

    with pytest.raises(GeocodingError, match="не существ"):
        await resolve_birth_data(birth)


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_ambiguous_dst_local_time():
    birth = BirthInput(
        birth_date="2024-10-27",
        birth_time="03:30",
        time_precision=TimePrecision.EXACT,
        birth_place="Kyiv, Ukraine",
        birth_place_latitude=50.4501,
        birth_place_longitude=30.5234,
        birth_place_timezone="Europe/Kyiv",
    )

    with pytest.raises(GeocodingError, match="неоднознач"):
        await resolve_birth_data(birth)


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


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_invalid_embedded_timezone_without_network():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Odesa, Ukraine",
        birth_place_geoname_id="698740",
        birth_place_latitude=46.47747,
        birth_place_longitude=30.73262,
        birth_place_timezone="Invalid/Timezone",
        birth_place_display_name="Odesa, Ukraine",
    )

    with pytest.raises(GeocodingError, match="часовой пояс"):
        await resolve_birth_data(birth, geocoder=geocoder)

    assert geocoder.called is False


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_invalid_embedded_coordinates_without_network():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Odesa, Ukraine",
        birth_place_geoname_id="698740",
        birth_place_latitude=120.0,
        birth_place_longitude=30.73262,
        birth_place_timezone="Europe/Kyiv",
        birth_place_display_name="Odesa, Ukraine",
    )

    with pytest.raises(GeocodingError, match="координаты"):
        await resolve_birth_data(birth, geocoder=geocoder)

    assert geocoder.called is False


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_invalid_geocoder_coordinates():
    geocoder = InvalidCoordinateGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Definitely Missing Natal City",
    )

    with pytest.raises(GeocodingError, match="координаты"):
        await resolve_birth_data(birth, geocoder=geocoder, geocoder_provider="nominatim")

    assert geocoder.called is True


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_approximate_time_without_value():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.APPROXIMATE,
        birth_place="Odesa, Ukraine",
        birth_place_geoname_id="698740",
        birth_place_latitude=46.47747,
        birth_place_longitude=30.73262,
        birth_place_timezone="Europe/Kyiv",
        birth_place_display_name="Odesa, Ukraine",
    )

    with pytest.raises(GeocodingError, match="примерное время"):
        await resolve_birth_data(birth, geocoder=geocoder)

    assert geocoder.called is False


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_range_time_without_end():
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.RANGE,
        birth_time_range_start="06:00",
        birth_place="Odesa, Ukraine",
        birth_place_geoname_id="698740",
        birth_place_latitude=46.47747,
        birth_place_longitude=30.73262,
        birth_place_timezone="Europe/Kyiv",
        birth_place_display_name="Odesa, Ukraine",
    )

    with pytest.raises(GeocodingError, match="Диапазон времени"):
        await resolve_birth_data(birth)


@pytest.mark.asyncio
async def test_resolve_birth_data_rejects_overnight_range_time():
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.RANGE,
        birth_time_range_start="23:30",
        birth_time_range_end="01:30",
        birth_place="Odesa, Ukraine",
        birth_place_geoname_id="698740",
        birth_place_latitude=46.47747,
        birth_place_longitude=30.73262,
        birth_place_timezone="Europe/Kyiv",
        birth_place_display_name="Odesa, Ukraine",
    )

    with pytest.raises(GeocodingError, match="Диапазон времени"):
        await resolve_birth_data(birth)


@pytest.mark.asyncio
async def test_resolve_birth_data_uses_birth_country_for_local_city_lookup_without_network():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Одесса",
        birth_place_country_code="UA",
    )

    resolved = await resolve_birth_data(birth, geocoder=geocoder)

    assert geocoder.called is False
    assert resolved.display_place == "Odesa, Odesa Oblast, Ukraine"
    assert resolved.timezone == "Europe/Kyiv"


@pytest.mark.asyncio
async def test_resolve_birth_data_local_provider_does_not_call_network_fallback():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Definitely Missing Natal City",
        birth_place_country_code="UA",
    )

    with pytest.raises(GeocodingError, match="локальном каталоге"):
        await resolve_birth_data(birth, geocoder=geocoder, geocoder_provider="local")

    assert geocoder.called is False


@pytest.mark.asyncio
async def test_resolve_birth_data_unknown_provider_does_not_call_network_fallback():
    geocoder = FakeGeocoder()
    birth = BirthInput(
        birth_date="1995-02-14",
        time_precision=TimePrecision.UNKNOWN,
        birth_place="Definitely Missing Natal City",
        birth_place_country_code="UA",
    )

    with pytest.raises(GeocodingError, match="локальном каталоге"):
        await resolve_birth_data(birth, geocoder=geocoder, geocoder_provider="nomninatim")

    assert geocoder.called is False


@pytest.mark.asyncio
async def test_nominatim_geocoder_caches_query_without_logging_place(monkeypatch, caplog):
    from cachetools import TTLCache

    from app.natal import geocoding

    private_place = "Private Birth Place 39f2"
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = [{"display_name": "Resolved Place", "lat": "48.1", "lon": "31.2"}]
    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(geocoding, "_nominatim_cache", TTLCache(maxsize=8, ttl=60), raising=False)
    monkeypatch.setattr(geocoding, "_nominatim_last_started_at", 0.0, raising=False)
    monkeypatch.setattr(geocoding.httpx, "AsyncClient", lambda **kwargs: client)

    geocoder = geocoding.NominatimGeocoder()
    with caplog.at_level(logging.DEBUG):
        first = await geocoder.geocode(private_place)
        second = await geocoder.geocode(private_place)

    assert first == second
    client.get.assert_awaited_once()
    assert private_place not in caplog.text


@pytest.mark.asyncio
async def test_nominatim_throttle_waits_for_global_one_second_slot(monkeypatch):
    from app.natal import geocoding

    clock = MagicMock(side_effect=[10.25, 11.0])
    sleep = AsyncMock()
    monkeypatch.setattr(geocoding, "_nominatim_last_started_at", 10.0, raising=False)
    monkeypatch.setattr(geocoding, "_monotonic", clock, raising=False)
    monkeypatch.setattr(geocoding.asyncio, "sleep", sleep, raising=False)

    await geocoding._wait_for_nominatim_slot()

    sleep.assert_awaited_once_with(pytest.approx(0.75))
    assert geocoding._nominatim_last_started_at == 11.0
