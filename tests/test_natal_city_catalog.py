import pytest

from app.natal.city_catalog import CityCatalog, CityRecord, find_city_by_id, search_cities, warm_city_catalog


def test_search_cities_matches_cyrillic_prefix_and_returns_timezone():
    results = search_cities("Оде", limit=5)

    assert results
    assert results[0].name in {"Odesa", "Odessa"}
    assert results[0].timezone == "Europe/Kyiv"
    assert results[0].latitude
    assert results[0].longitude


def test_search_cities_ranks_larger_exact_prefix_before_smaller_matches():
    catalog = CityCatalog(
        [
            CityRecord(
                geoname_id="small",
                name="Ottawa",
                country_code="US",
                admin1_code="",
                latitude=41.0,
                longitude=-91.0,
                timezone="America/Chicago",
                population=6000,
                alternatenames=("Оттава",),
            ),
            CityRecord(
                geoname_id="large",
                name="Ottawa",
                country_code="CA",
                admin1_code="08",
                latitude=45.4215,
                longitude=-75.6972,
                timezone="America/Toronto",
                population=1000000,
                alternatenames=("Оттава",),
            ),
        ]
    )

    results = catalog.search("Отт", limit=2)

    assert [city.geoname_id for city in results] == ["large", "small"]


def test_find_city_by_id_returns_packaged_record():
    city = find_city_by_id(search_cities("Оттава", limit=1)[0].geoname_id)

    assert city is not None
    assert city.timezone.startswith("America/")


def test_search_cities_rejects_one_character_noise():
    assert search_cities("О", limit=5)
    assert search_cities(" ", limit=5) == []


def test_warm_city_catalog_returns_city_count():
    assert warm_city_catalog() > 10000


@pytest.mark.parametrize(
    ("query", "timezone_prefix"),
    [
        ("Odesa", "Europe/"),
        ("Kyiv", "Europe/"),
        ("Moscow", "Europe/"),
        ("London", "Europe/"),
        ("New York", "America/"),
        ("Ottawa", "America/"),
        ("Orenburg", "Asia/"),
        ("Berlin", "Europe/"),
        ("Warsaw", "Europe/"),
        ("Istanbul", "Europe/"),
    ],
)
def test_search_cities_covers_release_smoke_set(query: str, timezone_prefix: str):
    results = search_cities(query, limit=5)

    assert results, query
    assert results[0].timezone.startswith(timezone_prefix)
