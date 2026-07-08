from datetime import UTC, datetime

from app.natal.astronomy import (
    calculate_ascendant,
    calculate_mc,
    julian_day,
    local_sidereal_time_degrees,
    mean_obliquity_degrees,
)


def test_julian_day_matches_j2000_epoch():
    assert julian_day(datetime(2000, 1, 1, 12, 0, tzinfo=UTC)) == 2451545.0


def test_sidereal_time_and_obliquity_match_j2000_reference_values():
    epoch = datetime(2000, 1, 1, 12, 0, tzinfo=UTC)

    assert abs(local_sidereal_time_degrees(epoch, longitude=0.0) - 280.46061837) < 0.0001
    assert abs(mean_obliquity_degrees(epoch) - 23.439291) < 0.0001


def test_ascendant_and_mc_are_stable_for_kyiv_reference_case():
    utc_dt = datetime.fromisoformat("1995-02-14T04:30:00+00:00")

    ascendant = calculate_ascendant(utc_dt, latitude=50.4501, longitude=30.5234)
    mc = calculate_mc(utc_dt, longitude=30.5234)

    assert abs(ascendant - 304.6123) < 0.05
    assert abs(mc - 243.7638) < 0.05


def test_ascendant_changes_with_latitude():
    utc_dt = datetime.fromisoformat("1995-02-14T04:30:00+00:00")

    kyiv = calculate_ascendant(utc_dt, latitude=50.4501, longitude=30.5234)
    equator = calculate_ascendant(utc_dt, latitude=0.0, longitude=30.5234)

    assert abs(kyiv - equator) > 1.0
