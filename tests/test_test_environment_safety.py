"""The unit-test process must never inherit production service credentials."""

from __future__ import annotations

import os

import pytest

from tests.e2e import conftest as e2e_conftest
from tests.integration import conftest as integration_conftest


def test_unit_tests_force_non_production_credentials():
    expected = {
        "TELEGRAM_BOT_TOKEN": "1234567890:dummy-token-for-tests-only",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/testdb",
        "GEMINI_API_KEYS": "dummy-gemini-key-for-tests",
        "TAVILY_API_KEYS": "dummy-tavily-key-for-tests",
        "ELEVENLABS_API_KEYS": "",
        "POLLINATIONS_API_KEY": "",
        "JINA_API_KEY": "",
        "WEATHER_API_KEY": "",
        "EXCHANGE_RATE_API_KEY": "",
        "FREETHEAI_API_KEYS": "",
    }
    unsafe_names = [name for name, safe_value in expected.items() if os.environ.get(name) != safe_value]

    assert not unsafe_names, f"Production-like credentials were loaded for: {', '.join(unsafe_names)}"


@pytest.mark.parametrize("db_conftest", [integration_conftest, e2e_conftest])
def test_database_identity_ignores_credentials(db_conftest):
    production = "postgresql://production_user:secret@db.example.com:5432/app"
    test_alias = "postgresql://different_user:other@db.example.com:5432/app"

    assert db_conftest._database_identity(test_alias) == db_conftest._database_identity(production)


@pytest.mark.parametrize("db_conftest", [integration_conftest, e2e_conftest])
def test_database_identity_normalizes_default_postgres_port(db_conftest):
    implicit_port = "postgresql://user:secret@db.example.com/app"
    explicit_port = "postgresql://user:secret@db.example.com:5432/app"

    assert db_conftest._database_identity(implicit_port) == db_conftest._database_identity(explicit_port)
