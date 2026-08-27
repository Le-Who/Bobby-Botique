"""
Local astrological engine based on ephem.
Calculates basic planetary positions and moon phases for Gemini prompts.
"""

import math
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


def is_retrograde(body, observer: ephem.Observer, dt: datetime) -> bool:
    """
    Check if a planetary body is in retrograde motion.
    Retrograde means the ecliptic longitude is decreasing.
    """
    # Position at dt
    observer.date = dt
    body.compute(observer)
    lon1 = ephem.Ecliptic(body).lon

    # Position at dt + 1 day
    observer.date = dt + timedelta(days=1)
    body.compute(observer)
    lon2 = ephem.Ecliptic(body).lon

    # Handle wrap-around at 360 degrees (0 radians)
    diff = lon2 - lon1
    if diff < -math.pi:
        diff += 2 * math.pi
    elif diff > math.pi:
        diff -= 2 * math.pi

    return diff < 0


def get_astro_context(dt: datetime | None = None) -> str:
    """
    Returns a formatted string containing the current astrological transits
    for the given datetime. Suitable for injecting into LLM system prompts.
    """
    if dt is None:
        dt = datetime.now(UTC)

    observer = ephem.Observer()
    observer.date = dt

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

    sun_lon = ephem.Ecliptic(sun).lon
    moon_lon = ephem.Ecliptic(moon).lon
    merc_lon = ephem.Ecliptic(mercury).lon
    ven_lon = ephem.Ecliptic(venus).lon
    mars_lon = ephem.Ecliptic(mars).lon

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
