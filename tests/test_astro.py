from datetime import UTC, datetime, timedelta, timezone

import ephem
import pytest

from app import astro


@pytest.mark.parametrize(
    ("timestamp", "expected_sign"),
    [
        ("2023-03-20T20:24:00+00:00", "Рыбы"),
        ("2023-03-20T22:24:00+00:00", "Овен"),
        ("2025-03-20T10:01:00+00:00", "Овен"),
    ],
)
def test_astro_context_sun_sign_follows_equinox_of_date(timestamp, expected_sign):
    # NWS/USNO: March equinoxes at 2023-03-20 21:24Z and 2025-03-20 09:01Z.
    # https://www.weather.gov/fwd/astrodata (CDT is UTC-5).
    context = astro.get_astro_context(datetime.fromisoformat(timestamp))

    assert f"- Солнце в знаке: {expected_sign}\n" in context


@pytest.mark.parametrize("timestamp", ["2023-03-20T21:24:00+00:00", "2025-03-20T09:01:00+00:00"])
def test_ecliptic_longitude_is_zero_at_published_march_equinox(timestamp):
    # Independent equinox times above; allow rounding to a minute and local engine error.
    longitude = astro.ecliptic_longitude(ephem.Sun, datetime.fromisoformat(timestamp))
    distance_from_equinox = min(longitude, 360.0 - longitude)

    assert distance_from_equinox < 0.002


def test_ecliptic_longitude_normalizes_timezone_to_utc():
    utc_dt = datetime(2023, 3, 20, 22, 24, tzinfo=UTC)
    offset_dt = utc_dt.astimezone(timezone(timedelta(hours=14)))

    assert astro.ecliptic_longitude(ephem.Sun, offset_dt) == pytest.approx(
        astro.ecliptic_longitude(ephem.Sun, utc_dt), abs=1e-10
    )
    assert astro.ecliptic_longitude(ephem.Sun, utc_dt.replace(tzinfo=None)) == pytest.approx(
        astro.ecliptic_longitude(ephem.Sun, ephem.Date(utc_dt)), abs=1e-10
    )


def test_retrograde_check_preserves_observer_and_computed_body():
    observer = ephem.Observer()
    observer.date = datetime(2023, 8, 29, 6, tzinfo=UTC)
    observer.lat = "50.45"
    observer.lon = "30.52"
    mercury = ephem.Mercury()
    mercury.compute(observer)
    original_date = float(observer.date)
    original_ra = float(mercury.ra)
    original_dec = float(mercury.dec)

    retrograde = astro.is_retrograde(mercury, observer, datetime(2023, 8, 30, tzinfo=UTC))

    assert retrograde is True
    assert float(observer.date) == original_date
    assert float(mercury.ra) == original_ra
    assert float(mercury.dec) == original_dec
