from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.astro import ecliptic_longitude
from app.natal.astronomy import angular_distance, normalize_longitude
from app.natal.calculator import _ASPECTS, _PLANETS, _allowed_orb, calculate_chart
from app.natal.geocoding import GeocodingError, resolve_birth_data
from app.natal.models import BirthInput, ChartData, TimePrecision

type LongitudeSamples = Mapping[str, Sequence[float]]


@dataclass(frozen=True)
class PartnerChart:
    chart: ChartData | None
    signs: dict[str, tuple[int, ...]]


@dataclass(frozen=True)
class SynastryAspect:
    point_a: str
    point_b: str
    aspect: str
    orb: float | None


@dataclass(frozen=True)
class HouseOverlay:
    planet: str
    source_partner: int
    target_partner: int
    house: int


@dataclass(frozen=True)
class SynastryData:
    first: PartnerChart
    second: PartnerChart
    aspects: list[SynastryAspect]
    overlays: list[HouseOverlay]


async def calculate_synastry(first: BirthInput, second: BirthInput) -> SynastryData:
    """Calculate locally, keeping catalog loading and numerical work off the event loop."""
    return await asyncio.to_thread(_calculate_in_thread, first, second)


def _calculate_in_thread(first: BirthInput, second: BirthInput) -> SynastryData:
    # The reused natal boundaries are async, but local resolution and calculation
    # perform synchronous work. Their private event loop belongs to this worker.
    return asyncio.run(_calculate_partners(first, second))


async def _calculate_partners(first: BirthInput, second: BirthInput) -> SynastryData:
    partner_a, samples_a = await _partner_chart(first)
    partner_b, samples_b = await _partner_chart(second)
    uncertain = TimePrecision.UNKNOWN in {first.time_precision, second.time_precision}
    aspects = inter_chart_aspects(samples_a, samples_b, uncertain=uncertain)
    overlays = stable_house_overlays(samples_a, partner_b.chart, source_partner=0, target_partner=1)
    overlays.extend(stable_house_overlays(samples_b, partner_a.chart, source_partner=1, target_partner=0))
    return SynastryData(partner_a, partner_b, aspects, overlays)


async def _partner_chart(birth: BirthInput) -> tuple[PartnerChart, dict[str, tuple[float, ...]]]:
    date.fromisoformat(birth.birth_date)
    has_city = bool(birth.birth_place.strip()) or all(
        value is not None
        for value in (birth.birth_place_latitude, birth.birth_place_longitude, birth.birth_place_timezone)
    )
    if not has_city and birth.time_precision != TimePrecision.UNKNOWN:
        raise GeocodingError("Место рождения обязательно, если время рождения известно.")

    chart: ChartData | None = None
    timezone: str | None = None
    if has_city:
        resolved = await resolve_birth_data(birth, geocoder_provider="local")
        chart = await calculate_chart(resolved)
        timezone = resolved.timezone
    if birth.time_precision == TimePrecision.UNKNOWN:
        start, end = possible_birth_interval(birth, timezone=timezone)
        samples = _sample_planets(start, end)
        signs = _possible_signs(samples)
        if chart is not None:
            chart.input_quality.moon_uncertainty = len(signs["moon"]) > 1 or chart.input_quality.moon_uncertainty
            # UNKNOWN has no representative instant that can be published as
            # precise degrees, retrograde status or natal aspect orbs.
            chart.planets = []
            chart.aspects = []
    else:
        assert chart is not None
        samples = {planet.key: (planet.longitude,) for planet in chart.planets}
        signs = _possible_signs(samples)
        if chart.input_quality.angles_available:
            samples.update({key: (longitude,) for key, longitude in chart.angles.items()})
    return PartnerChart(chart, signs), samples


def possible_birth_interval(birth: BirthInput, *, timezone: str | None = None) -> tuple[datetime, datetime]:
    """Return all possible UTC instants of an unknown-time local birth date.

    A missing city admits offsets from UTC+14 through UTC-12, extending the
    UTC interval from 14 hours before that date to 36 hours after it starts.
    """
    birth_date = date.fromisoformat(birth.birth_date)
    midnight = datetime.combine(birth_date, time(), tzinfo=UTC)
    if timezone is None:
        return midnight - timedelta(hours=14), midnight + timedelta(hours=36)

    zone = ZoneInfo(timezone)
    local_start = datetime.combine(birth_date, time())
    local_end = local_start + timedelta(days=1)
    # Considering both folds makes unusual midnight transitions conservative.
    starts = [local_start.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    ends = [local_end.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)]
    return min(starts), max(ends) - timedelta(microseconds=1)


def _sample_planets(start: datetime, end: datetime) -> dict[str, tuple[float, ...]]:
    instants = [start]
    sample = start + timedelta(minutes=30)
    while sample < end:
        instants.append(sample)
        sample += timedelta(minutes=30)
    if end != start:
        instants.append(end)
    return {
        key: tuple(ecliptic_longitude(body_factory, instant) for instant in instants)
        for key, _label, body_factory in _PLANETS
    }


def _longitude_interval(samples: Sequence[float]) -> tuple[float, float]:
    """Unwrap chronological samples, retaining intermediate extrema and crossings."""
    current = normalize_longitude(samples[0])
    lower = upper = current
    for longitude in samples[1:]:
        current += (normalize_longitude(longitude) - current + 180.0) % 360.0 - 180.0
        lower = min(lower, current)
        upper = max(upper, current)
    return lower, upper


def _possible_signs(samples: LongitudeSamples) -> dict[str, tuple[int, ...]]:
    signs: dict[str, tuple[int, ...]] = {}
    for key, longitudes in samples.items():
        lower, upper = _longitude_interval(longitudes)
        signs[key] = tuple(sorted({index % 12 for index in range(math.floor(lower / 30), math.floor(upper / 30) + 1)}))
    return signs


def _distance_interval(first: tuple[float, float], second: tuple[float, float]) -> tuple[float, float]:
    lower = first[0] - second[1]
    upper = first[1] - second[0]
    distances = [angular_distance(lower, 0.0), angular_distance(upper, 0.0)]
    # The triangular angular-distance function changes direction at each
    # multiple of 180 degrees. Endpoints alone would miss these extrema.
    for multiple in range(math.ceil(lower / 180), math.floor(upper / 180) + 1):
        distances.append(180.0 if multiple % 2 else 0.0)
    return min(distances), max(distances)


def inter_chart_aspects(
    first: LongitudeSamples, second: LongitudeSamples, *, uncertain: bool = False
) -> list[SynastryAspect]:
    """Keep aspects inside the natal orb policy for both independent sample envelopes."""
    intervals_a = {key: _longitude_interval(samples) for key, samples in first.items() if samples}
    intervals_b = {key: _longitude_interval(samples) for key, samples in second.items() if samples}
    result: list[SynastryAspect] = []
    for point_a, interval_a in intervals_a.items():
        for point_b, interval_b in intervals_b.items():
            lower, upper = _distance_interval(interval_a, interval_b)
            for aspect, angle in _ASPECTS:
                allowed = _allowed_orb(point_a, point_b, aspect)
                if max(abs(lower - angle), abs(upper - angle)) <= allowed:
                    orb = None if uncertain else round(abs(lower - angle), 2)
                    result.append(SynastryAspect(point_a, point_b, aspect, orb))
                    break
    return result


def stable_house_overlays(
    source: LongitudeSamples,
    target: ChartData | None,
    *,
    source_partner: int,
    target_partner: int,
) -> list[HouseOverlay]:
    """Assign planets only when their whole envelope occupies one known equal house."""
    if target is None or not target.input_quality.houses_available or len(target.houses) != 12:
        return []
    ascendant = target.houses[0].cusp_longitude
    result: list[HouseOverlay] = []
    planet_keys = {key for key, _label, _factory in _PLANETS}
    for planet, samples in source.items():
        if planet not in planet_keys or not samples:
            continue
        lower, upper = _longitude_interval(samples)
        first_house = math.floor((lower - ascendant) / 30)
        last_house = math.floor((upper - ascendant) / 30)
        if first_house == last_house:
            result.append(HouseOverlay(planet, source_partner, target_partner, first_house % 12 + 1))
    return result
