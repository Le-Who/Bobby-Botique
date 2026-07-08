from app.natal.city_readiness import AUTOCOMPLETE_READINESS_LIMIT, check_city_catalog_readiness, format_city_readiness


class FakeCity:
    def __init__(self, name: str, timezone: str = "Europe/Kyiv") -> None:
        self.name = name
        self.display_name = f"{name}, Test"
        self.latitude = 46.47747
        self.longitude = 30.73262
        self.timezone = timezone


def test_check_city_catalog_readiness_reports_release_city_results():
    def fake_warm_city_catalog() -> int:
        return 32444

    def fake_search_cities(query: str, limit: int = 8, country_code: str | None = None):
        if query == "Missing":
            return []
        return [FakeCity(query)]

    result = check_city_catalog_readiness(
        search_cities_fn=fake_search_cities,
        warm_city_catalog_fn=fake_warm_city_catalog,
        cases=(("Odesa", "Europe/"), ("Missing", "Europe/")),
        autocomplete_cases=(),
        disambiguation_cases=(),
    )

    assert result.city_count == 32444
    assert result.passed is False
    assert result.checked_cases == 2
    assert result.failures == ["Missing: no local city match"]


def test_format_city_readiness_includes_timings_and_failures():
    def fake_warm_city_catalog() -> int:
        return 32444

    def fake_search_cities(query: str, limit: int = 8, country_code: str | None = None):
        return [FakeCity(query, timezone="America/Toronto")]

    result = check_city_catalog_readiness(
        search_cities_fn=fake_search_cities,
        warm_city_catalog_fn=fake_warm_city_catalog,
        cases=(("Ottawa", "America/"),),
        autocomplete_cases=(),
        disambiguation_cases=(),
    )

    output = format_city_readiness(result)

    assert "PASS natal-city-catalog" in output
    assert "cities=32444" in output
    assert "warmup_ms=" in output
    assert "search_ms=" in output
    assert "Ottawa -> Ottawa, Test" in output


def test_check_city_catalog_readiness_fails_when_search_exceeds_threshold():
    def fake_warm_city_catalog() -> int:
        return 32444

    def fake_search_cities(query: str, limit: int = 8, country_code: str | None = None):
        return [FakeCity(query)]

    result = check_city_catalog_readiness(
        search_cities_fn=fake_search_cities,
        warm_city_catalog_fn=fake_warm_city_catalog,
        cases=(("Odesa", "Europe/"),),
        autocomplete_cases=(),
        disambiguation_cases=(),
        max_search_ms=0.0,
    )

    assert result.passed is False
    assert any("search exceeded" in failure for failure in result.failures)


def test_check_city_catalog_readiness_fails_when_catalog_is_too_small():
    def fake_warm_city_catalog() -> int:
        return 1000

    def fake_search_cities(query: str, limit: int = 8, country_code: str | None = None):
        return [FakeCity(query)]

    result = check_city_catalog_readiness(
        search_cities_fn=fake_search_cities,
        warm_city_catalog_fn=fake_warm_city_catalog,
        cases=(("Odesa", "Europe/"),),
        autocomplete_cases=(),
        disambiguation_cases=(),
    )

    assert result.passed is False
    assert any("catalog city count below" in failure for failure in result.failures)


def test_check_city_catalog_readiness_verifies_country_filtered_autocomplete_narrows_results():
    calls = []

    def fake_warm_city_catalog() -> int:
        return 32444

    def fake_search_cities(query: str, limit: int = 8, country_code: str | None = None):
        calls.append((query, limit, country_code))
        if query == "О" and country_code == "UA":
            return [FakeCity("Odesa"), FakeCity("Oleksandriia"), FakeCity("Okhtyrka")]
        if query == "Оде" and country_code == "UA":
            return [FakeCity("Odesa")]
        return [FakeCity(query)]

    result = check_city_catalog_readiness(
        search_cities_fn=fake_search_cities,
        warm_city_catalog_fn=fake_warm_city_catalog,
        cases=(("Odesa", "Europe/"),),
        autocomplete_cases=(("О", "Оде", "UA", "Odesa"),),
        disambiguation_cases=(),
    )

    assert result.passed is True
    assert ("О", AUTOCOMPLETE_READINESS_LIMIT, "UA") in calls
    assert ("Оде", AUTOCOMPLETE_READINESS_LIMIT, "UA") in calls
    assert result.checked_cases == 2


def test_check_city_catalog_readiness_verifies_region_disambiguation():
    calls = []

    def fake_warm_city_catalog() -> int:
        return 32444

    def fake_search_cities(query: str, limit: int = 8, country_code: str | None = None):
        calls.append((query, limit, country_code))
        if query == "Reading, Massachusetts" and country_code == "US":
            city = FakeCity("Reading", timezone="America/New_York")
            city.display_name = "Reading, Massachusetts, United States"
            return [city]
        return [FakeCity(query)]

    result = check_city_catalog_readiness(
        search_cities_fn=fake_search_cities,
        warm_city_catalog_fn=fake_warm_city_catalog,
        cases=(),
        autocomplete_cases=(),
        disambiguation_cases=(("Reading, Massachusetts", "US", "Reading, Massachusetts, United States"),),
    )

    assert result.passed is True
    assert ("Reading, Massachusetts", 5, "US") in calls
    assert result.checked_cases == 1
