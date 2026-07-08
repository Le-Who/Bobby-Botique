from types import SimpleNamespace

from app.natal.config_readiness import check_natal_config_readiness, format_natal_config_readiness


def test_natal_config_readiness_passes_release_safe_settings():
    settings = SimpleNamespace(
        NATAL_REPORTS_ENABLED=True,
        NATAL_REPORT_TTL_DAYS=365,
        NATAL_GEOCODER_PROVIDER="local",
        NATAL_CITY_OVERRIDES_PATH="",
        NATAL_SEND_RAW_BIRTH_DATA_TO_LLM=False,
        ENABLE_WEB_SERVER=True,
    )

    result = check_natal_config_readiness(
        settings,
        webhook_url="https://bot.example.com",
        package_versions={"geonamescache": "3.0.1", "ephem": "4.2", "tzdata": "2025.2"},
        python_version=(3, 14, 3),
    )

    assert result.passed is True
    assert format_natal_config_readiness(result) == "PASS natal-config: ready"


def test_natal_config_readiness_rejects_public_release_misconfiguration():
    settings = SimpleNamespace(
        NATAL_REPORTS_ENABLED=False,
        NATAL_REPORT_TTL_DAYS=0,
        NATAL_GEOCODER_PROVIDER="nominatim",
        NATAL_CITY_OVERRIDES_PATH="",
        NATAL_SEND_RAW_BIRTH_DATA_TO_LLM=True,
        ENABLE_WEB_SERVER=False,
    )

    result = check_natal_config_readiness(
        settings,
        webhook_url="http://bot.example.com",
        package_versions={"geonamescache": "3.0.1", "ephem": "4.2", "tzdata": "2025.2"},
        python_version=(3, 14, 3),
    )

    assert result.passed is False
    output = format_natal_config_readiness(result)
    assert "NATAL_REPORTS_ENABLED must be true" in output
    assert "NATAL_REPORT_TTL_DAYS must be positive" in output
    assert "NATAL_GEOCODER_PROVIDER must be local" in output
    assert "NATAL_SEND_RAW_BIRTH_DATA_TO_LLM must remain false" in output
    assert "ENABLE_WEB_SERVER must be true" in output
    assert "WEBHOOK_URL must use https://" in output


def test_natal_config_readiness_rejects_missing_or_incompatible_runtime_dependencies():
    settings = SimpleNamespace(
        NATAL_REPORTS_ENABLED=True,
        NATAL_REPORT_TTL_DAYS=365,
        NATAL_GEOCODER_PROVIDER="local",
        NATAL_CITY_OVERRIDES_PATH="",
        NATAL_SEND_RAW_BIRTH_DATA_TO_LLM=False,
        ENABLE_WEB_SERVER=True,
    )

    result = check_natal_config_readiness(
        settings,
        webhook_url="https://bot.example.com",
        package_versions={"geonamescache": "2.0.0", "ephem": None},
        python_version=(3, 13, 9),
    )

    assert result.passed is False
    output = format_natal_config_readiness(result)
    assert "Python 3.14+" in output
    assert "geonamescache must be installed with version >=3.0.1,<4.0.0" in output
    assert "ephem must be installed with version >=4.1.0,<5.0.0" in output
    assert "tzdata must be installed" in output


def test_natal_config_readiness_validates_city_override_file(tmp_path):
    settings = SimpleNamespace(
        NATAL_REPORTS_ENABLED=True,
        NATAL_REPORT_TTL_DAYS=365,
        NATAL_GEOCODER_PROVIDER="local",
        NATAL_CITY_OVERRIDES_PATH=str(tmp_path / "missing.json"),
        NATAL_SEND_RAW_BIRTH_DATA_TO_LLM=False,
        ENABLE_WEB_SERVER=True,
    )

    result = check_natal_config_readiness(
        settings,
        webhook_url="https://bot.example.com",
        package_versions={"geonamescache": "3.0.1", "ephem": "4.2", "tzdata": "2025.2"},
        python_version=(3, 14, 3),
    )

    assert result.passed is False
    assert "NATAL_CITY_OVERRIDES_PATH" in format_natal_config_readiness(result)
