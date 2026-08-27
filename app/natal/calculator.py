from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import ephem

from app.natal.astronomy import (
    angular_distance,
    calculate_ascendant,
    calculate_mc,
    normalize_longitude,
)
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
        ascendant = calculate_ascendant(utc_dt, resolved.latitude, resolved.longitude)
        mc = calculate_mc(utc_dt, resolved.longitude)
        angles = {"ascendant": ascendant, "mc": mc}
        houses = []
        for number in range(1, 13):
            cusp = normalize_longitude(ascendant + (number - 1) * 30.0)
            houses.append(House(number=number, cusp_longitude=cusp, sign=_sign_for(cusp)))
        _assign_houses(planets, houses)
        warnings.append("Использована equal-house система домов от рассчитанного Асцендента.")
        if precision == TimePrecision.APPROXIMATE:
            warnings.append("Время рождения примерное: дома и углы показаны с ограниченной точностью.")
        elif precision == TimePrecision.RANGE:
            warnings.append("Использован midpoint диапазона времени; дома и углы приблизительны.")
    else:
        warnings.append("Время рождения неизвестно: дома, Асцендент и MC не рассчитываются как достоверные.")
    moon_uncertainty = precision == TimePrecision.UNKNOWN and _moon_uncertain_for_unknown_time(resolved)
    if moon_uncertainty:
        warnings.append("Время рождения неизвестно: знак или аспекты Луны могут отличаться в течение дня.")

    return ChartData(
        input_quality=InputQuality(
            time_precision=precision,
            houses_available=houses_available,
            angles_available=houses_available,
            moon_uncertainty=moon_uncertainty,
            warnings=warnings,
        ),
        planets=planets,
        aspects=aspects,
        houses=houses,
        angles=angles,
    )


def _planet_position(
    key: str,
    label: str,
    body_factory,
    ephem_date: ephem.Date,
    *,
    include_retrograde: bool = True,
) -> PlanetPosition:
    body = body_factory(ephem_date)
    ecliptic = ephem.Ecliptic(body)
    longitude = normalize_longitude(math.degrees(float(ecliptic.lon)))
    return PlanetPosition(
        key=key,
        label=label,
        longitude=longitude,
        sign=_sign_for(longitude),
        degree_in_sign=longitude % 30.0,
        retrograde=include_retrograde and _is_retrograde(body_factory, ephem_date),
    )


def _is_retrograde(body_factory, ephem_date: ephem.Date) -> bool:
    before = _body_longitude(body_factory, ephem.Date(ephem_date.datetime() - timedelta(hours=12)))
    after = _body_longitude(body_factory, ephem.Date(ephem_date.datetime() + timedelta(hours=12)))
    movement = (after - before + 180.0) % 360.0 - 180.0
    return movement < 0.0


def _body_longitude(body_factory, ephem_date: ephem.Date) -> float:
    body = body_factory(ephem_date)
    return normalize_longitude(math.degrees(float(ephem.Ecliptic(body).lon)))


def _moon_uncertain_for_unknown_time(resolved: ResolvedBirthData) -> bool:
    local_datetime = datetime.fromisoformat(resolved.local_datetime.replace("Z", "+00:00"))
    day_start_local = local_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = local_datetime.replace(hour=23, minute=59, second=59, microsecond=0)
    day_start_utc = day_start_local.astimezone(UTC).replace(tzinfo=None)
    day_end_utc = day_end_local.astimezone(UTC).replace(tzinfo=None)

    start_sign = int(_body_longitude(ephem.Moon, ephem.Date(day_start_utc)) // 30.0)
    end_sign = int(_body_longitude(ephem.Moon, ephem.Date(day_end_utc)) // 30.0)
    if start_sign != end_sign:
        return True

    start_moon_aspects = _moon_aspect_signature(ephem.Date(day_start_utc))
    end_moon_aspects = _moon_aspect_signature(ephem.Date(day_end_utc))
    return start_moon_aspects != end_moon_aspects


def _moon_aspect_signature(ephem_date: ephem.Date) -> set[tuple[str, str, str]]:
    planets = [
        _planet_position(key, label, body_factory, ephem_date, include_retrograde=False)
        for key, label, body_factory in _PLANETS
    ]
    return {
        (aspect.point_a, aspect.point_b, aspect.aspect)
        for aspect in _calculate_aspects(planets)
        if aspect.point_a == "moon" or aspect.point_b == "moon"
    }


def _calculate_aspects(planets: list[PlanetPosition]) -> list[Aspect]:
    aspects: list[Aspect] = []
    for index, first in enumerate(planets):
        for second in planets[index + 1 :]:
            distance = angular_distance(first.longitude, second.longitude)
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
        offset = normalize_longitude(planet.longitude - asc)
        planet.house = int(offset // 30.0) + 1


def _sign_for(longitude: float) -> str:
    return _SIGNS[int(normalize_longitude(longitude) // 30.0)]
