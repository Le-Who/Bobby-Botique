from __future__ import annotations

import math
from datetime import datetime

import ephem

from app.natal.models import (
    Aspect,
    ChartData,
    House,
    InputQuality,
    PlanetPosition,
    ResolvedBirthData,
    TimePrecision,
)

_PLANETS = [
    ("sun", "Солнце", ephem.Sun),
    ("moon", "Луна", ephem.Moon),
    ("mercury", "Меркурий", ephem.Mercury),
    ("venus", "Венера", ephem.Venus),
    ("mars", "Марс", ephem.Mars),
    ("jupiter", "Юпитер", ephem.Jupiter),
    ("saturn", "Сатурн", ephem.Saturn),
    ("uranus", "Уран", ephem.Uranus),
    ("neptune", "Нептун", ephem.Neptune),
    ("pluto", "Плутон", ephem.Pluto),
]

_SIGNS = [
    "Овен",
    "Телец",
    "Близнецы",
    "Рак",
    "Лев",
    "Дева",
    "Весы",
    "Скорпион",
    "Стрелец",
    "Козерог",
    "Водолей",
    "Рыбы",
]

_ASPECTS = [
    ("conjunction", 0.0),
    ("sextile", 60.0),
    ("square", 90.0),
    ("trine", 120.0),
    ("opposition", 180.0),
]

_DEG_TO_RAD = math.pi / 180.0
_RAD_TO_DEG = 180.0 / math.pi


async def calculate_chart(resolved: ResolvedBirthData) -> ChartData:
    precision = resolved.birth_input.time_precision
    houses_available = precision != TimePrecision.UNKNOWN
    utc_dt = datetime.fromisoformat(resolved.utc_datetime.replace("Z", "+00:00"))
    ephem_date = ephem.Date(utc_dt.replace(tzinfo=None))
    planets = [_planet_position(key, label, body_factory, ephem_date) for key, label, body_factory in _PLANETS]
    aspects = _calculate_aspects(planets)
    angles: dict[str, float] = {}
    houses: list[House] = []
    warnings: list[str] = []

    if houses_available:
        ascendant = _calculate_ascendant(utc_dt, resolved.latitude, resolved.longitude)
        mc = _calculate_mc(utc_dt, resolved.longitude)
        angles = {"ascendant": ascendant, "mc": mc}
        houses = []
        for number in range(1, 13):
            cusp = _normalize_longitude(ascendant + (number - 1) * 30.0)
            houses.append(House(number=number, cusp_longitude=cusp, sign=_sign_for(cusp)))
        _assign_houses(planets, houses)
        warnings.append("Использована equal-house система домов от рассчитанного Асцендента.")
        if precision == TimePrecision.APPROXIMATE:
            warnings.append("Время рождения примерное: дома и углы показаны с ограниченной точностью.")
        elif precision == TimePrecision.RANGE:
            warnings.append("Использован midpoint диапазона времени; дома и углы приблизительны.")
    else:
        warnings.append("Время рождения неизвестно: дома, Асцендент и MC не рассчитываются как достоверные.")

    return ChartData(
        input_quality=InputQuality(
            time_precision=precision,
            houses_available=houses_available,
            angles_available=houses_available,
            moon_uncertainty=precision == TimePrecision.UNKNOWN,
            warnings=warnings,
        ),
        planets=planets,
        aspects=aspects,
        houses=houses,
        angles=angles,
    )


def _planet_position(key: str, label: str, body_factory, ephem_date: ephem.Date) -> PlanetPosition:
    body = body_factory(ephem_date)
    ecliptic = ephem.Ecliptic(body)
    longitude = _normalize_longitude(math.degrees(float(ecliptic.lon)))
    return PlanetPosition(
        key=key,
        label=label,
        longitude=longitude,
        sign=_sign_for(longitude),
        degree_in_sign=longitude % 30.0,
        retrograde=False,
    )


def _calculate_aspects(planets: list[PlanetPosition]) -> list[Aspect]:
    aspects: list[Aspect] = []
    for index, first in enumerate(planets):
        for second in planets[index + 1 :]:
            distance = _angular_distance(first.longitude, second.longitude)
            for aspect_name, aspect_angle in _ASPECTS:
                orb = abs(distance - aspect_angle)
                allowed = _allowed_orb(first.key, second.key, aspect_name)
                if orb <= allowed:
                    aspects.append(
                        Aspect(
                            point_a=first.key,
                            point_b=second.key,
                            aspect=aspect_name,
                            orb=round(orb, 2),
                        )
                    )
                    break
    return aspects


def _allowed_orb(first: str, second: str, aspect_name: str) -> float:
    if aspect_name == "sextile":
        return 4.0
    if first in {"sun", "moon"} or second in {"sun", "moon"}:
        return 8.0
    return 6.0


def _assign_houses(planets: list[PlanetPosition], houses: list[House]) -> None:
    cusps = [house.cusp_longitude for house in houses]
    asc = cusps[0]
    for planet in planets:
        offset = _normalize_longitude(planet.longitude - asc)
        planet.house = int(offset // 30.0) + 1


def _calculate_ascendant(utc_dt: datetime, latitude: float, longitude: float) -> float:
    lst = _local_sidereal_time(utc_dt, longitude)
    obliquity = _mean_obliquity(utc_dt)
    lat_rad = latitude * _DEG_TO_RAD

    def altitude_at_ecliptic_longitude(ecliptic_longitude: float) -> float:
        ra, declination = _ecliptic_to_equatorial(ecliptic_longitude, obliquity)
        hour_angle = _normalize_radians(lst - ra)
        return (
            math.sin(lat_rad) * math.sin(declination)
            + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
        )

    roots = _find_ecliptic_roots(altitude_at_ecliptic_longitude)
    if not roots:
        return _calculate_mc(utc_dt, longitude)
    for root in roots:
        ra, _declination = _ecliptic_to_equatorial(root, obliquity)
        hour_angle = _normalize_radians(lst - ra)
        if math.sin(hour_angle) < 0:
            return root
    return roots[0]


def _calculate_mc(utc_dt: datetime, longitude: float) -> float:
    lst = _local_sidereal_time(utc_dt, longitude)
    obliquity = _mean_obliquity(utc_dt)

    def meridian_delta(ecliptic_longitude: float) -> float:
        ra, _declination = _ecliptic_to_equatorial(ecliptic_longitude, obliquity)
        return _normalize_radians(ra - lst)

    roots = _find_ecliptic_roots(meridian_delta)
    return roots[0] if roots else _normalize_longitude(lst * _RAD_TO_DEG)


def _local_sidereal_time(utc_dt: datetime, longitude: float) -> float:
    jd = _julian_day(utc_dt)
    t = (jd - 2451545.0) / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t * t
        - (t * t * t) / 38710000.0
    )
    return _normalize_longitude(gmst + longitude) * _DEG_TO_RAD


def _mean_obliquity(utc_dt: datetime) -> float:
    jd = _julian_day(utc_dt)
    t = (jd - 2451545.0) / 36525.0
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    epsilon = 23.0 + (26.0 / 60.0) + (seconds / 3600.0)
    return epsilon * _DEG_TO_RAD


def _julian_day(utc_dt: datetime) -> float:
    year = utc_dt.year
    month = utc_dt.month
    day = utc_dt.day
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + (a // 4)
    day_fraction = (
        utc_dt.hour
        + utc_dt.minute / 60.0
        + (utc_dt.second + utc_dt.microsecond / 1_000_000.0) / 3600.0
    ) / 24.0
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + day
        + day_fraction
        + b
        - 1524.5
    )


def _ecliptic_to_equatorial(ecliptic_longitude: float, obliquity: float) -> tuple[float, float]:
    longitude_rad = ecliptic_longitude * _DEG_TO_RAD
    ra = math.atan2(math.sin(longitude_rad) * math.cos(obliquity), math.cos(longitude_rad))
    declination = math.asin(math.sin(longitude_rad) * math.sin(obliquity))
    return _normalize_radians(ra), declination


def _find_ecliptic_roots(function) -> list[float]:
    roots: list[float] = []
    previous_longitude = 0.0
    previous_value = function(previous_longitude)
    for longitude in range(1, 361):
        current_longitude = float(longitude)
        current_value = function(0.0 if longitude == 360 else current_longitude)
        if previous_value == 0:
            roots.append(previous_longitude)
        elif previous_value * current_value < 0:
            roots.append(_bisect_ecliptic_root(function, previous_longitude, current_longitude))
        previous_longitude = current_longitude
        previous_value = current_value
    deduped: list[float] = []
    for root in roots:
        normalized = _normalize_longitude(root)
        if not any(_angular_distance(normalized, existing) < 0.01 for existing in deduped):
            deduped.append(normalized)
    return deduped


def _bisect_ecliptic_root(function, low: float, high: float) -> float:
    low_value = function(low)
    for _ in range(40):
        midpoint = (low + high) / 2.0
        mid_value = function(0.0 if midpoint >= 360.0 else midpoint)
        if low_value * mid_value <= 0:
            high = midpoint
        else:
            low = midpoint
            low_value = mid_value
    return _normalize_longitude((low + high) / 2.0)


def _sign_for(longitude: float) -> str:
    return _SIGNS[int(_normalize_longitude(longitude) // 30.0)]


def _normalize_longitude(value: float) -> float:
    return value % 360.0


def _normalize_radians(value: float) -> float:
    normalized = (value + math.pi) % (2.0 * math.pi) - math.pi
    return normalized


def _angular_distance(first: float, second: float) -> float:
    distance = abs(first - second) % 360.0
    return min(distance, 360.0 - distance)
