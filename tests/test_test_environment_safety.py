"""The unit-test process must never inherit production service credentials."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.e2e import conftest as e2e_conftest
from tests.integration import conftest as integration_conftest


@pytest.mark.parametrize("db_conftest", [integration_conftest, e2e_conftest])
async def test_database_key_fixture_matches_provider_quota_identity(db_conftest):
    """A selected plaintext key must refer to the same row when quota hashes it."""
    conn = SimpleNamespace(execute=AsyncMock())
    expected_hash = "95dafc45648663c1476b0c93ff45b8fc76520ab4dd280d70833d344dc51ff140"

    result_conn, key_hash = await db_conftest.db_conn_with_key.__wrapped__(conn)

    assert result_conn is conn
    assert key_hash == expected_hash
    conn.execute.assert_awaited_once_with(
        "INSERT INTO api_keys (api_key, key_hash) VALUES ($1, $2)",
        "test-gemini-key-12345",
        expected_hash,
    )


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


@pytest.mark.parametrize("db_conftest", [integration_conftest, e2e_conftest])
def test_matching_database_is_allowed_only_for_explicit_local_github_service(monkeypatch, db_conftest):
    local_service = "postgresql://test:test@localhost:5432/test"
    remote_database = "postgresql://test:test@db.example.com:5432/test"

    monkeypatch.delenv("GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert db_conftest._database_target_is_forbidden(local_service, local_service) is True

    monkeypatch.setenv("GEMAIBOT_TEST_DATABASE_IS_EPHEMERAL", "true")
    assert db_conftest._database_target_is_forbidden(local_service, local_service) is True

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert db_conftest._database_target_is_forbidden(local_service, local_service) is False
    assert db_conftest._database_target_is_forbidden(remote_database, remote_database) is True
