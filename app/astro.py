"""
Local astrological engine based on ephem.
Calculates basic planetary positions and moon phases for Gemini prompts.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import ephem

# Standard Zodiac signs (0° to 360°, 30° per sign)
ZODIAC_SIGNS = [
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


def get_zodiac_sign(lon_radians: float) -> str:
    """Convert ecliptic longitude in radians to a Zodiac sign."""
    lon_degrees = math.degrees(lon_radians) % 360
    sign_index = int(lon_degrees // 30)
    return ZODIAC_SIGNS[sign_index]


def ecliptic_longitude(body_factory: Callable[[ephem.Date], ephem.Body], dt: datetime | ephem.Date) -> float:
    """Return apparent geocentric tropical longitude of date in degrees [0, 360).

    Aware datetimes are converted to UTC; naive datetimes are treated as UTC.
    PyEphem's Ecliptic(body) uses astrometric J2000 coordinates by default,
    so apparent geocentric RA/Dec must be expressed at the requested epoch.
    """
    date = _ephem_date(dt)
    return _computed_ecliptic_longitude(body_factory(date), date)


def _ephem_date(dt: datetime | ephem.Date) -> ephem.Date:
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return ephem.Date(dt)


def _computed_ecliptic_longitude(body: ephem.Body, date: ephem.Date) -> float:
    equatorial = ephem.Equatorial(body.g_ra, body.g_dec, epoch=date)
    return math.degrees(float(ephem.Ecliptic(equatorial).lon)) % 360.0


def is_retrograde(body, observer: ephem.Observer, dt: datetime) -> bool:
    """
    Check if a planetary body is in retrograde motion.
    Retrograde means the ecliptic longitude is decreasing.
    Calculate on a copy to preserve the caller's body and observer state.
    """
    sample = body.copy()

    # Position at dt
    date = _ephem_date(dt)
    sample.compute(date)
    lon1 = _computed_ecliptic_longitude(sample, date)

    # Position at dt + 1 day
    next_date = _ephem_date(dt + timedelta(days=1))
    sample.compute(next_date)
    lon2 = _computed_ecliptic_longitude(sample, next_date)

    # Handle wrap-around at 360 degrees.
    diff = (lon2 - lon1 + 180.0) % 360.0 - 180.0
    return diff < 0


def get_astro_context(dt: datetime | None = None) -> str:
    """
    Returns a formatted string containing the current astrological transits
    for the given datetime. Suitable for injecting into LLM system prompts.
    """
    if dt is None:
        dt = datetime.now(UTC)

    date = _ephem_date(dt)
    observer = ephem.Observer()
    observer.date = date

    sun = ephem.Sun()
    moon = ephem.Moon()
    mercury = ephem.Mercury()
    venus = ephem.Venus()
    mars = ephem.Mars()
    jupiter = ephem.Jupiter()
    saturn = ephem.Saturn()

    bodies = [sun, moon, mercury, venus, mars, jupiter, saturn]
    for b in bodies:
        b.compute(observer)

    sun_lon = math.radians(_computed_ecliptic_longitude(sun, date))
    moon_lon = math.radians(_computed_ecliptic_longitude(moon, date))
    merc_lon = math.radians(_computed_ecliptic_longitude(mercury, date))
    ven_lon = math.radians(_computed_ecliptic_longitude(venus, date))
    mars_lon = math.radians(_computed_ecliptic_longitude(mars, date))

    moon_phase = moon.phase  # percentage illumination 0-100
    merc_retro = is_retrograde(mercury, observer, dt)
    ven_retro = is_retrograde(venus, observer, dt)
    mars_retro = is_retrograde(mars, observer, dt)

    context = (
        f"Астрономическая сводка на {dt.strftime('%Y-%m-%d')}:\n"
        f"- Солнце в знаке: {get_zodiac_sign(sun_lon)}\n"
        f"- Луна в знаке: {get_zodiac_sign(moon_lon)} (Освещенность: {moon_phase:.1f}%)\n"
        f"- Меркурий в знаке: {get_zodiac_sign(merc_lon)}{' [РЕТРОГРАДНЫЙ]' if merc_retro else ''}\n"
        f"- Венера в знаке: {get_zodiac_sign(ven_lon)}{' [РЕТРОГРАДНАЯ]' if ven_retro else ''}\n"
        f"- Марс в знаке: {get_zodiac_sign(mars_lon)}{' [РЕТРОГРАДНЫЙ]' if mars_retro else ''}\n"
    )
    return context
