from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime

DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi


def julian_day(utc_dt: datetime) -> float:
    year = utc_dt.year
    month = utc_dt.month
    day = utc_dt.day
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + (a // 4)
    day_fraction = (
        utc_dt.hour + utc_dt.minute / 60.0 + (utc_dt.second + utc_dt.microsecond / 1_000_000.0) / 3600.0
    ) / 24.0
    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + day_fraction + b - 1524.5


def local_sidereal_time_degrees(utc_dt: datetime, longitude: float) -> float:
    jd = julian_day(utc_dt)
    t = (jd - 2451545.0) / 36525.0
    gmst = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - (t * t * t) / 38710000.0
    return normalize_longitude(gmst + longitude)


def mean_obliquity_degrees(utc_dt: datetime) -> float:
    jd = julian_day(utc_dt)
    t = (jd - 2451545.0) / 36525.0
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    return 23.0 + (26.0 / 60.0) + (seconds / 3600.0)


def calculate_ascendant(utc_dt: datetime, latitude: float, longitude: float) -> float:
    lst = local_sidereal_time_degrees(utc_dt, longitude) * DEG_TO_RAD
    obliquity = mean_obliquity_degrees(utc_dt) * DEG_TO_RAD
    lat_rad = latitude * DEG_TO_RAD

    def altitude_at_ecliptic_longitude(ecliptic_longitude: float) -> float:
        ra, declination = ecliptic_to_equatorial(ecliptic_longitude, obliquity)
        hour_angle = normalize_radians(lst - ra)
        return math.sin(lat_rad) * math.sin(declination) + math.cos(lat_rad) * math.cos(declination) * math.cos(
            hour_angle
        )

    roots = find_ecliptic_roots(altitude_at_ecliptic_longitude)
    if not roots:
        return calculate_mc(utc_dt, longitude)
    for root in roots:
        ra, _declination = ecliptic_to_equatorial(root, obliquity)
        hour_angle = normalize_radians(lst - ra)
        if math.sin(hour_angle) < 0:
            return root
    return roots[0]


def calculate_mc(utc_dt: datetime, longitude: float) -> float:
    lst = local_sidereal_time_degrees(utc_dt, longitude) * DEG_TO_RAD
    obliquity = mean_obliquity_degrees(utc_dt) * DEG_TO_RAD

    def meridian_delta(ecliptic_longitude: float) -> float:
        ra, _declination = ecliptic_to_equatorial(ecliptic_longitude, obliquity)
        return normalize_radians(ra - lst)

    roots = find_ecliptic_roots(meridian_delta)
    if not roots:
        return normalize_longitude(lst * RAD_TO_DEG)
    return min(
        roots,
        key=lambda root: angular_distance(
            ecliptic_to_equatorial(root, obliquity)[0] * RAD_TO_DEG,
            lst * RAD_TO_DEG,
        ),
    )


def ecliptic_to_equatorial(ecliptic_longitude: float, obliquity: float) -> tuple[float, float]:
    longitude_rad = ecliptic_longitude * DEG_TO_RAD
    ra = math.atan2(math.sin(longitude_rad) * math.cos(obliquity), math.cos(longitude_rad))
    declination = math.asin(math.sin(longitude_rad) * math.sin(obliquity))
    return normalize_radians(ra), declination


def find_ecliptic_roots(function: Callable[[float], float]) -> list[float]:
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
        normalized = normalize_longitude(root)
        if not any(angular_distance(normalized, existing) < 0.01 for existing in deduped):
            deduped.append(normalized)
    return deduped


def normalize_longitude(value: float) -> float:
    return value % 360.0


def normalize_radians(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def angular_distance(first: float, second: float) -> float:
    distance = abs(first - second) % 360.0
    return min(distance, 360.0 - distance)


def _bisect_ecliptic_root(function: Callable[[float], float], low: float, high: float) -> float:
    low_value = function(low)
    for _ in range(40):
        midpoint = (low + high) / 2.0
        mid_value = function(0.0 if midpoint >= 360.0 else midpoint)
        if low_value * mid_value <= 0:
            high = midpoint
        else:
            low = midpoint
            low_value = mid_value
    return normalize_longitude((low + high) / 2.0)
