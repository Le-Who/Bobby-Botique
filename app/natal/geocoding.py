from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from time import monotonic as _monotonic
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cachetools import TTLCache

from app.natal.city_catalog import nearest_city_timezone, search_cities
from app.natal.models import BirthInput, ResolvedBirthData, TimePrecision

logger = logging.getLogger(__name__)

_NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
_nominatim_cache: TTLCache[str, GeocodeResult] = TTLCache(maxsize=512, ttl=24 * 60 * 60)
_nominatim_lock = asyncio.Lock()
_nominatim_last_started_at = 0.0


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
        cache_key = " ".join(query.split()).casefold()
        cached = _nominatim_cache.get(cache_key)
        if cached is not None:
            return cached

        async with _nominatim_lock:
            cached = _nominatim_cache.get(cache_key)
            if cached is not None:
                return cached
            await _wait_for_nominatim_slot()
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
            result = GeocodeResult(
                display_name=str(first.get("display_name") or query),
                latitude=latitude,
                longitude=longitude,
            )
            _nominatim_cache[cache_key] = result
            return result


async def _wait_for_nominatim_slot() -> None:
    global _nominatim_last_started_at
    remaining = _NOMINATIM_MIN_INTERVAL_SECONDS - (_monotonic() - _nominatim_last_started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)
    _nominatim_last_started_at = _monotonic()


async def resolve_birth_data(
    birth: BirthInput,
    geocoder: GeocoderProtocol | None = None,
    geocoder_provider: str = "local",
) -> ResolvedBirthData:
    provider = (geocoder_provider or "local").strip().lower()
    embedded = _embedded_geocode_result(birth)
    if embedded is not None:
        result, timezone_name = embedded
    else:
        local = _local_city_result(birth.birth_place, birth.birth_place_country_code)
        if local is not None:
            result, timezone_name = local
        else:
            if provider != "nominatim":
                raise GeocodingError("Место рождения не найдено в локальном каталоге. Выберите страну и ближайший город.")
            geocoder = geocoder or NominatimGeocoder()
            result = await geocoder.geocode(birth.birth_place)
            _validate_coordinates(result.latitude, result.longitude)
            timezone_name = _resolve_timezone(result.latitude, result.longitude, result.display_name)
    _validate_coordinates(result.latitude, result.longitude)
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise GeocodingError(f"Не удалось определить часовой пояс для места рождения: {timezone_name}.") from exc
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
    city = None
    for query in _local_city_queries(place):
        matches = search_cities(query, limit=1, country_code=country_code)
        if matches:
            city = matches[0]
            break
    if city is None:
        return None
    return (
        GeocodeResult(
            display_name=city.display_name,
            latitude=city.latitude,
            longitude=city.longitude,
        ),
        city.timezone,
    )


def _local_city_queries(place: str) -> list[str]:
    query = place.strip()
    if not query:
        return []
    queries = [query]
    comma_prefix = query.split(",", 1)[0].strip()
    if comma_prefix and comma_prefix != query:
        queries.append(comma_prefix)
    return queries


def _build_local_datetime(birth: BirthInput, local_zone: ZoneInfo) -> datetime:
    birth_date = datetime.strptime(birth.birth_date, "%Y-%m-%d").date()
    if birth.time_precision == TimePrecision.UNKNOWN:
        birth_time = time(12, 0)
    elif birth.time_precision == TimePrecision.RANGE:
        if not birth.birth_time_range_start or not birth.birth_time_range_end:
            raise GeocodingError("Диапазон времени должен содержать начало и конец.")
        start = _parse_time(birth.birth_time_range_start)
        end = _parse_time(birth.birth_time_range_end)
        start_minutes = start.hour * 60 + start.minute
        end_minutes = end.hour * 60 + end.minute
        if end_minutes <= start_minutes:
            raise GeocodingError("Диапазон времени должен быть в пределах одного дня.")
        midpoint_minutes = (start_minutes + end_minutes) // 2
        birth_time = time(midpoint_minutes // 60, midpoint_minutes % 60)
    elif birth.birth_time:
        birth_time = _parse_time(birth.birth_time)
    elif birth.time_precision == TimePrecision.EXACT:
        raise GeocodingError("Укажите точное время рождения.")
    elif birth.time_precision == TimePrecision.APPROXIMATE:
        raise GeocodingError("Укажите примерное время рождения.")
    else:
        birth_time = time(12, 0)  # type: ignore[unreachable]
    local_naive = datetime.combine(birth_date, birth_time)
    valid_candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = local_naive.replace(tzinfo=local_zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(local_zone).replace(tzinfo=None)
        if round_trip == local_naive:
            valid_candidates.append(candidate)

    if not valid_candidates:
        raise GeocodingError(
            "Выбранное местное время не существовало из-за перехода на летнее время. Укажите другое время."
        )
    if len(valid_candidates) == 2 and valid_candidates[0].utcoffset() != valid_candidates[1].utcoffset():
        raise GeocodingError(
            "Выбранное местное время неоднозначно из-за перехода с летнего времени. Уточните время рождения."
        )
    return valid_candidates[0]


def _parse_time(value: str) -> time:
    parsed = datetime.strptime(value, "%H:%M")
    return time(parsed.hour, parsed.minute)


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise GeocodingError("Геокодер вернул некорректные координаты места рождения.")


def _resolve_timezone(latitude: float, longitude: float, display_name: str) -> str:
    nearest_timezone = nearest_city_timezone(latitude, longitude)
    if nearest_timezone:
        return nearest_timezone
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
    raise GeocodingError("Не удалось определить часовой пояс для места рождения.")
