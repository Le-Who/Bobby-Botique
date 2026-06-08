import pytest

from app.natal import city_catalog
from app.natal.city_catalog import (
    CityCatalog,
    CityRecord,
    CountryRecord,
    find_city_by_id,
    load_city_overrides,
    search_cities,
    search_countries,
    warm_city_catalog,
)


def test_search_cities_matches_cyrillic_prefix_and_returns_timezone():
    results = search_cities("Оде", limit=5)

    assert results
    assert results[0].name in {"Odesa", "Odessa"}
    assert results[0].timezone == "Europe/Kyiv"
    assert results[0].latitude
    assert results[0].longitude


def test_search_cities_ranks_larger_exact_prefix_before_smaller_matches():
    catalog = CityCatalog(
        cities=[
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
        ],
        countries=[CountryRecord(code="CA", name="Canada", population=1000000)],
    )

    results = catalog.search("Отт", limit=2)

    assert [city.geoname_id for city in results] == ["large", "small"]


def test_search_cities_uses_prefix_candidate_index_for_autocomplete():
    cities = [
        CityRecord(
            geoname_id=f"beta-{index}",
            name=f"Beta {index}",
            country_code="US",
            admin1_code="",
            latitude=40.0,
            longitude=-75.0,
            timezone="America/New_York",
            population=index,
        )
        for index in range(50)
    ] + [
        CityRecord(
            geoname_id="odesa",
            name="Odesa",
            country_code="UA",
            admin1_code="17",
            latitude=46.48572,
            longitude=30.74383,
            timezone="Europe/Kyiv",
            population=1000000,
            alternatenames=("Одесса",),
        )
    ]
    catalog = CityCatalog(cities=cities)

    candidates = catalog.city_candidate_rows("Оде")
    results = catalog.search("Оде", limit=5)

    assert len(candidates) == 1
    assert len(candidates) < len(catalog._search_rows)
    assert [city.geoname_id for city in results] == ["odesa"]


def test_search_cities_falls_back_to_substring_scan_when_prefix_index_has_too_few_results():
    catalog = CityCatalog(
        cities=[
            CityRecord(
                geoname_id="new-york",
                name="New York",
                country_code="US",
                admin1_code="NY",
                latitude=40.71427,
                longitude=-74.00597,
                timezone="America/New_York",
                population=8000000,
            )
        ]
    )

    results = catalog.search("ork", limit=5)

    assert [city.geoname_id for city in results] == ["new-york"]


def test_find_city_by_id_returns_packaged_record():
    city = find_city_by_id(search_cities("Оттава", limit=1)[0].geoname_id)

    assert city is not None
    assert city.timezone.startswith("America/")


def test_city_display_name_includes_region_when_known():
    city = CityRecord(
        geoname_id="reading-pa",
        name="Reading",
        country_code="US",
        admin1_code="PA",
        latitude=40.3356,
        longitude=-75.9269,
        timezone="America/New_York",
        population=95112,
        admin1_name="Pennsylvania",
    )

    assert city.display_name == "Reading, Pennsylvania, United States"


def test_packaged_us_city_display_name_includes_state_for_disambiguation():
    results = search_cities("Reading", limit=10, country_code="US")
    reading_pa = next(city for city in results if city.name == "Reading" and city.admin1_code == "PA")

    assert reading_pa.display_name == "Reading, Pennsylvania, United States"


def test_search_cities_prefers_exact_city_name_over_similar_substring():
    results = search_cities("Reading", limit=5, country_code="US")

    assert results[0].name == "Reading"


def test_search_cities_uses_admin_region_to_disambiguate_same_name_city():
    results = search_cities("Reading, Massachusetts", limit=5, country_code="US")

    assert results
    assert results[0].name == "Reading"
    assert results[0].admin1_code == "MA"


def test_search_countries_matches_cyrillic_prefix():
    results = search_countries("У", limit=5)

    assert results
    assert results[0].code == "UA"


@pytest.mark.parametrize(
    ("query", "country_code"),
    [
        ("Франция", "FR"),
        ("Испания", "ES"),
        ("Италия", "IT"),
        ("Грузия", "GE"),
        ("Армения", "AM"),
        ("Молдова", "MD"),
        ("Нидерланды", "NL"),
        ("Чехия", "CZ"),
        ("Сербия", "RS"),
        ("Латвия", "LV"),
    ],
)
def test_search_countries_matches_common_russian_country_names(query: str, country_code: str):
    results = search_countries(query, limit=5)

    assert results
    assert results[0].code == country_code


@pytest.mark.parametrize(
    ("country_code", "expected_name"),
    [
        ("UA", "Odesa"),
        ("RU", "Orenburg"),
        ("CA", "Ottawa"),
    ],
)
def test_search_cities_suggests_popular_city_from_one_letter_inside_country(country_code: str, expected_name: str):
    results = search_cities("О", limit=8, country_code=country_code)
    names = {city.name for city in results}

    assert expected_name in names
    assert search_cities(" ", limit=5) == []


def test_search_cities_narrows_results_when_user_adds_letters():
    broad_results = search_cities("О", limit=30, country_code="UA")
    narrow_results = search_cities("Оде", limit=5, country_code="UA")

    assert len(narrow_results) < len(broad_results)
    assert narrow_results[0].name in {"Odesa", "Odessa"}


def test_warm_city_catalog_returns_city_count():
    assert warm_city_catalog() > 10000


def test_load_city_overrides_returns_searchable_local_records(tmp_path):
    path = tmp_path / "natal-city-overrides.json"
    path.write_text(
        """
        {
          "cities": [
            {
              "geoname_id": "manual-odesa-suburb",
              "name": "Таирово",
              "country_code": "UA",
              "admin1_name": "Odesa Oblast",
              "latitude": 46.3900,
              "longitude": 30.7050,
              "timezone": "Europe/Kyiv",
              "population": 10000,
              "alternatenames": ["Tairove", "Таїрове"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    records = load_city_overrides(path)
    catalog = CityCatalog(
        cities=records,
        countries=[CountryRecord(code="UA", name="Ukraine", population=1, alternatenames=("Украина",))],
    )
    results = catalog.search("Таи", limit=5, country_code="UA")

    assert results[0].geoname_id == "manual-odesa-suburb"
    assert results[0].latitude == 46.39
    assert results[0].timezone == "Europe/Kyiv"


@pytest.mark.parametrize("path", [None, "", " ", "."])
def test_load_city_overrides_treats_empty_path_as_disabled(path):
    assert load_city_overrides(path) == []


def test_global_city_catalog_includes_env_override(monkeypatch, tmp_path):
    path = tmp_path / "natal-city-overrides.json"
    path.write_text(
        """
        {
          "cities": [
            {
              "geoname_id": "manual-test-city",
              "name": "Codexgrad",
              "country_code": "UA",
              "latitude": 46.1000,
              "longitude": 30.2000,
              "timezone": "Europe/Kyiv"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("NATAL_CITY_OVERRIDES_PATH", str(path))
    city_catalog._catalog.cache_clear()

    try:
        results = search_cities("Codex", limit=5, country_code="UA")
    finally:
        monkeypatch.delenv("NATAL_CITY_OVERRIDES_PATH")
        city_catalog._catalog.cache_clear()

    assert results
    assert results[0].geoname_id == "manual-test-city"


def test_load_city_overrides_rejects_invalid_coordinates_or_timezone(tmp_path):
    path = tmp_path / "bad-city-overrides.json"
    path.write_text(
        """
        {
          "cities": [
            {
              "geoname_id": "bad-city",
              "name": "Bad City",
              "country_code": "UA",
              "latitude": 146.0,
              "longitude": 30.0,
              "timezone": "Definitely/Missing"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad-city"):
        load_city_overrides(path)


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
