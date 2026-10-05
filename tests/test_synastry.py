from datetime import UTC, datetime, timedelta
from threading import get_ident

import pytest

from app.natal import synastry
from app.natal.geocoding import GeocodingError
from app.natal.models import BirthInput, ChartData, House, InputQuality, TimePrecision


def birth(precision=TimePrecision.EXACT, *, birth_date="1997-11-09", city=True, **updates):
    fields = {
        "birth_date": birth_date,
        "time_precision": precision,
        "birth_time": "03:00" if precision != TimePrecision.UNKNOWN else None,
    }
    if city:
        fields.update(
            birth_place="Odesa, Ukraine",
            birth_place_latitude=46.47747,
            birth_place_longitude=30.73262,
            birth_place_timezone="Europe/Kyiv",
        )
    fields.update(updates)
    return BirthInput(**fields)


def equal_house_chart(ascendant=330.0, *, available=True):
    return ChartData(
        input_quality=InputQuality(
            time_precision=TimePrecision.EXACT if available else TimePrecision.UNKNOWN,
            houses_available=available,
            angles_available=available,
        ),
        planets=[],
        aspects=[],
        houses=[
            House(number=number, cusp_longitude=(ascendant + 30 * (number - 1)) % 360, sign="")
            for number in range(1, 13)
        ]
        if available
        else [],
        angles={"ascendant": ascendant, "mc": 240.0} if available else {},
    )


@pytest.mark.parametrize(
    ("longitude", "aspect", "orb"),
    [
        (2.0, "conjunction", 2.0),
        (63.0, "sextile", 3.0),
        (94.0, "square", 4.0),
        (125.0, "trine", 5.0),
        (178.0, "opposition", 2.0),
    ],
)
def test_inter_chart_aspects_follow_fixed_longitude_geometry(longitude, aspect, orb):
    result = synastry.inter_chart_aspects({"venus": (0.0,)}, {"mars": (longitude,)})

    assert result == [synastry.SynastryAspect("venus", "mars", aspect, orb)]


def test_inter_chart_aspects_wrap_zero_and_include_known_angles():
    result = synastry.inter_chart_aspects({"ascendant": (359.0,)}, {"moon": (1.0,), "mc": (179.0,)})

    assert result == [
        synastry.SynastryAspect("ascendant", "moon", "conjunction", 2.0),
        synastry.SynastryAspect("ascendant", "mc", "opposition", 0.0),
    ]


@pytest.mark.parametrize(
    ("first", "second", "longitude", "expected"),
    [
        ("sun", "mars", 8.0, "conjunction"),
        ("venus", "mars", 8.0, None),
        ("sun", "mars", 65.0, None),
        ("sun", "mars", 64.0, "sextile"),
        ("sun", "mars", 35.0, None),
    ],
)
def test_inter_chart_aspects_reuse_natal_orb_policy(first, second, longitude, expected):
    result = synastry.inter_chart_aspects({first: (0.0,)}, {second: (longitude,)})

    assert [item.aspect for item in result] == ([expected] if expected else [])


def test_unknown_aspect_keeps_only_stable_geometry_and_hides_exact_orb():
    stable = synastry.inter_chart_aspects({"sun": (359.0,)}, {"moon": (355.0, 0.0, 3.0)}, uncertain=True)
    changing = synastry.inter_chart_aspects({"sun": (0.0,)}, {"moon": (0.0, 9.0)}, uncertain=True)

    assert stable == [synastry.SynastryAspect("sun", "moon", "conjunction", None)]
    assert changing == []


def test_unknown_aspects_inspect_intermediate_samples_and_independent_partner_times():
    interior_change = synastry.inter_chart_aspects({"sun": (0.0,)}, {"moon": (0.0, 30.0, 0.0)}, uncertain=True)
    independent_times = synastry.inter_chart_aspects(
        {"moon": (350.0, 0.0, 10.0)}, {"moon": (350.0, 0.0, 10.0)}, uncertain=True
    )

    assert interior_change == []
    assert independent_times == []


def test_unknown_aspects_include_angular_distance_turning_points_inside_sample_envelope():
    crossing_conjunction = synastry.inter_chart_aspects({"sun": (0.0,)}, {"moon": (86.0, 274.0)}, uncertain=True)
    crossing_opposition = synastry.inter_chart_aspects({"sun": (0.0,)}, {"moon": (173.0, 187.0)}, uncertain=True)

    assert crossing_conjunction == []
    assert crossing_opposition == [synastry.SynastryAspect("sun", "moon", "opposition", None)]


def test_house_overlays_handle_wrap_and_omit_planets_crossing_house_cusp():
    result = synastry.stable_house_overlays(
        {"sun": (350.0, 355.0), "venus": (9.0, 11.0), "moon": (359.0, 1.0)},
        equal_house_chart(),
        source_partner=1,
        target_partner=0,
    )

    assert result == [
        synastry.HouseOverlay("sun", 1, 0, 1),
        synastry.HouseOverlay("venus", 1, 0, 2),
    ]


def test_house_overlays_require_available_target_houses():
    assert (
        synastry.stable_house_overlays(
            {"sun": (10.0,)}, equal_house_chart(available=False), source_partner=0, target_partner=1
        )
        == []
    )


def test_date_only_birth_interval_covers_all_possible_timezone_offsets():
    start, end = synastry.possible_birth_interval(birth(TimePrecision.UNKNOWN, city=False))

    assert start == datetime(1997, 11, 8, 10, tzinfo=UTC)
    assert end == datetime(1997, 11, 10, 12, tzinfo=UTC)


def test_city_unknown_birth_interval_follows_local_dst_day():
    start, end = synastry.possible_birth_interval(
        birth(TimePrecision.UNKNOWN, birth_date="2024-03-10"), timezone="America/New_York"
    )

    assert start == datetime(2024, 3, 10, 5, tzinfo=UTC)
    assert end == datetime(2024, 3, 11, 4, tzinfo=UTC) - timedelta(microseconds=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("precision", "updates"),
    [
        (TimePrecision.EXACT, {"birth_time": None}),
        (TimePrecision.APPROXIMATE, {"birth_time": None}),
        (TimePrecision.RANGE, {"birth_time_range_start": "02:00"}),
    ],
)
async def test_known_time_requires_requested_time_or_both_range_bounds(precision, updates):
    with pytest.raises(GeocodingError):
        await synastry.calculate_synastry(birth(precision, **updates), birth())


@pytest.mark.asyncio
async def test_known_time_requires_city_and_unknown_city_never_contacts_network():
    with pytest.raises(GeocodingError):
        await synastry.calculate_synastry(birth(city=False), birth())
    with pytest.raises(GeocodingError, match="локальном каталоге"):
        await synastry.calculate_synastry(
            birth(TimePrecision.UNKNOWN, city=False, birth_place="Missing Offline City 39f2"), birth()
        )


@pytest.mark.asyncio
async def test_exact_embedded_births_produce_full_charts_angles_and_bidirectional_overlays():
    result = await synastry.calculate_synastry(birth(), birth())

    assert result.first.chart is not None
    assert result.second.chart is not None
    assert len(result.first.chart.houses) == len(result.second.chart.houses) == 12
    assert result.first.chart.input_quality.angles_available is True
    assert result.first.signs["sun"] == (7,)
    assert result.first.signs["moon"] == (11,)
    conjunctions = {(item.point_a, item.point_b): item.orb for item in result.aspects if item.aspect == "conjunction"}
    assert conjunctions[("sun", "sun")] == 0.0
    assert conjunctions[("moon", "moon")] == 0.0
    assert conjunctions[("ascendant", "ascendant")] == 0.0
    assert conjunctions[("mc", "mc")] == 0.0
    assert len(result.overlays) == 20
    assert {(item.source_partner, item.target_partner) for item in result.overlays} == {(0, 1), (1, 0)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("precision", "updates"),
    [
        (TimePrecision.APPROXIMATE, {}),
        (
            TimePrecision.RANGE,
            {"birth_time": None, "birth_time_range_start": "02:00", "birth_time_range_end": "04:00"},
        ),
    ],
)
async def test_approximate_and_range_births_keep_natal_quality_and_midpoint_geometry(precision, updates):
    result = await synastry.calculate_synastry(birth(precision, **updates), birth())

    assert result.first.chart is not None
    assert result.first.chart.input_quality.time_precision == precision
    assert result.first.chart.input_quality.angles_available is True
    assert synastry.SynastryAspect("moon", "moon", "conjunction", 0.0) in result.aspects
    assert len(result.overlays) == 20


@pytest.mark.asyncio
async def test_unknown_city_time_preserves_other_chart_and_uses_uncertain_moon_signs():
    result = await synastry.calculate_synastry(birth(), birth(TimePrecision.UNKNOWN, birth_date="1995-02-01"))

    assert result.first.chart is not None
    assert result.first.chart.input_quality.angles_available is True
    assert len(result.first.chart.houses) == 12
    assert result.second.chart is not None
    assert result.second.chart.input_quality.angles_available is False
    assert result.second.chart.input_quality.houses_available is False
    assert result.second.chart.input_quality.moon_uncertainty is True
    assert result.second.chart.angles == {}
    assert result.second.chart.houses == []
    assert result.second.chart.planets == []
    assert result.second.chart.aspects == []
    assert len(result.second.signs["moon"]) == 2
    assert all(item.orb is None for item in result.aspects)
    assert all(item.point_b not in {"ascendant", "mc"} for item in result.aspects)
    assert all(item.target_partner == 0 for item in result.overlays)


@pytest.mark.asyncio
async def test_unknown_time_same_day_keeps_stable_sun_conjunction_but_omits_unstable_moon():
    result = await synastry.calculate_synastry(birth(), birth(TimePrecision.UNKNOWN))

    assert synastry.SynastryAspect("sun", "sun", "conjunction", None) in result.aspects
    assert not any(item.point_a == item.point_b == "moon" for item in result.aspects)


@pytest.mark.asyncio
async def test_date_only_partners_have_no_chart_and_report_sun_sign_boundary_uncertainty():
    result = await synastry.calculate_synastry(
        birth(TimePrecision.UNKNOWN, city=False, birth_date="2023-03-20"),
        birth(TimePrecision.UNKNOWN, city=False, birth_date="1995-02-01"),
    )

    assert result.first.chart is result.second.chart is None
    assert result.first.signs["sun"] == (0, 11)
    assert len(result.second.signs["moon"]) >= 2
    assert all(item.orb is None for item in result.aspects)
    assert result.overlays == []


@pytest.mark.asyncio
async def test_local_city_lookup_and_calculation_run_off_the_event_loop(monkeypatch):
    main_thread = get_ident()
    original = synastry.resolve_birth_data

    async def resolve_in_worker(*args, **kwargs):
        assert get_ident() != main_thread
        return await original(*args, **kwargs)

    monkeypatch.setattr(synastry, "resolve_birth_data", resolve_in_worker)
    local = birth(city=False, birth_place="Odesa, Ukraine", birth_place_country_code="UA")
    result = await synastry.calculate_synastry(local, birth())

    assert result.first.chart is not None
    assert result.first.signs["sun"] == (7,)


@pytest.mark.asyncio
async def test_date_only_ephemeris_sampling_runs_off_the_event_loop(monkeypatch):
    main_thread = get_ident()
    original = synastry.ecliptic_longitude

    def longitude_in_worker(*args, **kwargs):
        assert get_ident() != main_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(synastry, "ecliptic_longitude", longitude_in_worker)
    result = await synastry.calculate_synastry(birth(TimePrecision.UNKNOWN, city=False), birth())

    assert result.first.signs["sun"] == (7,)


@pytest.mark.asyncio
async def test_offline_calculation_is_deterministic():
    first = birth(TimePrecision.UNKNOWN, city=False)
    second = birth()

    assert await synastry.calculate_synastry(first, second) == await synastry.calculate_synastry(first, second)
