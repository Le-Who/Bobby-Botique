from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from app.natal.city_catalog import search_cities
from app.natal.models import BirthInput, ResolvedBirthData, TimePrecision

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeocodeResult:
    display_name: str
    latitude: float
    longitude: float


class GeocodingError(RuntimeError):
    pass


class GeocoderProtocol(Protocol):
    async def geocode(self, place: str) -> GeocodeResult:
        ...


class NominatimGeocoder:
    def __init__(self, user_agent: str = "GemAI Bot natal chart geocoder") -> None:
        self.user_agent = user_agent

    async def geocode(self, place: str) -> GeocodeResult:
        query = place.strip()
        if not query:
            raise GeocodingError("Place query is empty.")
        logger.debug("Geocoding place query: %s", query)
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 1},
                headers={"User-Agent": self.user_agent},
            )
            response.raise_for_status()
            data = response.json()
        if not data:
            raise GeocodingError("Место рождения не найдено.")
        first = data[0]
        try:
            latitude = float(first["lat"])
            longitude = float(first["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GeocodingError("Геокодер вернул некорректные координаты.") from exc
        return GeocodeResult(
            display_name=str(first.get("display_name") or query),
            latitude=latitude,
            longitude=longitude,
        )


async def resolve_birth_data(
    birth: BirthInput,
    geocoder: GeocoderProtocol | None = None,
) -> ResolvedBirthData:
    embedded = _embedded_geocode_result(birth)
    if embedded is not None:
        result, timezone_name = embedded
    else:
        local = _local_city_result(birth.birth_place, birth.birth_place_country_code)
        if local is not None:
            result, timezone_name = local
        else:
            geocoder = geocoder or NominatimGeocoder()
            result = await geocoder.geocode(birth.birth_place)
            timezone_name = _resolve_timezone(result.latitude, result.longitude, result.display_name)
    local_zone = ZoneInfo(timezone_name)
    local_dt = _build_local_datetime(birth, local_zone)
    utc_dt = local_dt.astimezone(UTC)
    return ResolvedBirthData(
        birth_input=birth,
        latitude=result.latitude,
        longitude=result.longitude,
        timezone=timezone_name,
        local_datetime=local_dt.isoformat(),
        utc_datetime=utc_dt.isoformat(),
        display_place=result.display_name,
    )


def _embedded_geocode_result(birth: BirthInput) -> tuple[GeocodeResult, str] | None:
    if (
        birth.birth_place_latitude is None
        or birth.birth_place_longitude is None
        or not birth.birth_place_timezone
    ):
        return None
    return (
        GeocodeResult(
            display_name=birth.birth_place_display_name or birth.birth_place,
            latitude=birth.birth_place_latitude,
            longitude=birth.birth_place_longitude,
        ),
        birth.birth_place_timezone,
    )


def _local_city_result(place: str, country_code: str | None = None) -> tuple[GeocodeResult, str] | None:
    matches = search_cities(place, limit=1, country_code=country_code)
    if not matches:
        return None
    city = matches[0]
    return (
        GeocodeResult(
            display_name=city.display_name,
            latitude=city.latitude,
            longitude=city.longitude,
        ),
        city.timezone,
    )


def _build_local_datetime(birth: BirthInput, local_zone: ZoneInfo) -> datetime:
    birth_date = datetime.strptime(birth.birth_date, "%Y-%m-%d").date()
    if birth.time_precision == TimePrecision.UNKNOWN:
        birth_time = time(12, 0)
    elif birth.time_precision == TimePrecision.RANGE and birth.birth_time_range_start and birth.birth_time_range_end:
        start = _parse_time(birth.birth_time_range_start)
        end = _parse_time(birth.birth_time_range_end)
        midpoint_minutes = ((start.hour * 60 + start.minute) + (end.hour * 60 + end.minute)) // 2
        birth_time = time(midpoint_minutes // 60, midpoint_minutes % 60)
    elif birth.birth_time:
        birth_time = _parse_time(birth.birth_time)
    else:
        birth_time = time(12, 0)
    return datetime.combine(birth_date, birth_time, tzinfo=local_zone)


def _parse_time(value: str) -> time:
    parsed = datetime.strptime(value, "%H:%M")
    return time(parsed.hour, parsed.minute)


def _resolve_timezone(latitude: float, longitude: float, display_name: str) -> str:
    name = display_name.lower()
    if "kyiv" in name or "kiev" in name or "київ" in name or "киев" in name or "ukraine" in name or "украина" in name:
        return "Europe/Kyiv"
    if "moscow" in name or "russia" in name:
        return "Europe/Moscow"
    if "london" in name or "united kingdom" in name:
        return "Europe/London"
    if "new york" in name:
        return "America/New_York"
    if "los angeles" in name:
        return "America/Los_Angeles"
    if 44.0 <= latitude <= 53.0 and 22.0 <= longitude <= 41.0:
        return "Europe/Kyiv"
    return "UTC"
