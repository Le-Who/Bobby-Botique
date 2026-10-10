"""Provider overrides across real crypto, settings cache and consumer boundaries.

All credentials and payloads here are synthetic. Only the database/network
boundaries are replaced; provider readers, encryption and cache writers stay real.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from cachetools import TTLCache

from app import config, crypto
from app.repos import provider_keys, settings_repo

PROVIDERS = ("weather", "exchange", "pollinations", "jina")
ENV_KEYS = {
    "weather": "synthetic-env-weather-0001",
    "exchange": "synthetic-env-exchange-0002",
    "pollinations": "synthetic-env-pollinations-0003",
    "jina": "synthetic-env-jina-0004",
}


@dataclass
class SettingsDatabase:
    """Stateful global_settings SQL boundary, without a database connection."""

    rows: dict[str, str] = field(default_factory=dict)

    async def query(self, sql: str, params: tuple = ()) -> list[dict[str, str]]:
        normalized = " ".join(sql.split())
        if normalized == "SELECT value_data FROM global_settings WHERE key_name = $1":
            assert len(params) == 1
            return [{"value_data": self.rows[params[0]]}] if params[0] in self.rows else []
        if normalized.startswith("INSERT INTO global_settings (key_name, value_data, updated_at)"):
            assert "ON CONFLICT (key_name) DO UPDATE" in normalized
            assert len(params) == 2
            self.rows[params[0]] = params[1]
            return []
        if normalized == "DELETE FROM global_settings WHERE key_name = $1":
            assert len(params) == 1
            self.rows.pop(params[0], None)
            return []
        if normalized.startswith(
            ("CREATE TABLE IF NOT EXISTS global_settings", "ALTER TABLE global_settings", "DO $$")
        ):
            assert params == ()
            return []
        raise AssertionError("Unexpected database operation in provider override contract")


@pytest.fixture
def provider_database(monkeypatch):
    database = SettingsDatabase()
    monkeypatch.setattr(settings_repo.db, "db_query", database.query)
    monkeypatch.setattr(settings_repo, "_cache", TTLCache(maxsize=64, ttl=30))
    monkeypatch.setattr(settings_repo, "_table_verified", False)
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(config.settings, "ADMIN_SECRET", "synthetic-provider-current-admin-secret")
    for name, value in (
        ("WEATHER_API_KEY", ENV_KEYS["weather"]),
        ("EXCHANGE_RATE_API_KEY", ENV_KEYS["exchange"]),
        ("POLLINATIONS_API_KEY", ENV_KEYS["pollinations"]),
        ("JINA_API_KEY", ENV_KEYS["jina"]),
    ):
        monkeypatch.setattr(config.settings, name, value)
    return database


@pytest.fixture(params=["changed_admin_secret", "damaged_ciphertext"])
def unreadable_override(request, provider_database, monkeypatch):
    if request.param == "damaged_ciphertext":
        return "gAAAAA" + "A" * 90
    with monkeypatch.context() as previous_config:
        previous_config.setattr(config.settings, "ADMIN_SECRET", "synthetic-provider-previous-admin-secret")
        previous_config.setattr(crypto, "_fernet_instance", None)
        return crypto.encrypt_api_key("synthetic-previous-db-provider-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_unreadable_db_override_does_not_fall_back_to_nonempty_env(
    provider_database, unreadable_override, provider
):
    # Break caught: decryption failure returns the environment credential.
    provider_database.rows[f"provider_key:{provider}"] = unreadable_override

    assert await provider_keys.get_provider_key(provider) == ""
    assert await provider_keys.get_provider_status(provider) == {
        "source": "missing",
        "preview": "ошибка расшифровки",
    }
    assert provider_database.rows[f"provider_key:{provider}"] == unreadable_override


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "preview"),
    [("weather", "synt…0001"), ("exchange", "synt…0002"), ("pollinations", "synt…0003"), ("jina", "synt…0004")],
)
async def test_absent_override_uses_environment_credential_and_status(provider_database, provider, preview):
    # Break caught: treating an absent row as an unreadable override.
    assert await provider_keys.get_provider_key(provider) == ENV_KEYS[provider]
    assert await provider_keys.get_provider_status(provider) == {"source": "env", "preview": preview}
    assert provider_database.rows == {}


@pytest.mark.asyncio
async def test_real_set_replaces_cached_absence_and_cached_encrypted_override(provider_database):
    # Break caught: dropping set_global_setting's cache invalidation or encryption.
    assert await provider_keys.get_provider_key("weather") == "synthetic-env-weather-0001"

    await provider_keys.set_provider_key("weather", "synthetic-db-weather-first-1001")
    stored = provider_database.rows["provider_key:weather"]
    assert stored != "synthetic-db-weather-first-1001"
    assert "synthetic-db-weather-first-1001" not in stored
    assert crypto.is_encrypted(stored)
    assert crypto.decrypt_api_key(stored) == "synthetic-db-weather-first-1001"
    assert await provider_keys.get_provider_key("weather") == "synthetic-db-weather-first-1001"
    assert await provider_keys.get_provider_status("weather") == {"source": "db", "preview": "synt…1001"}

    await provider_keys.set_provider_key("weather", "synthetic-db-weather-second-1002")
    assert await provider_keys.get_provider_key("weather") == "synthetic-db-weather-second-1002"
    assert await provider_keys.get_provider_status("weather") == {"source": "db", "preview": "synt…1002"}


@pytest.mark.asyncio
async def test_real_clear_removes_override_and_invalidates_cached_value(provider_database):
    # Break caught: deleting the row without evicting the previous override.
    await provider_keys.set_provider_key("jina", "synthetic-db-jina-2001")
    assert await provider_keys.get_provider_key("jina") == "synthetic-db-jina-2001"

    await provider_keys.clear_provider_key("jina")

    assert "provider_key:jina" not in provider_database.rows
    assert await provider_keys.get_provider_key("jina") == "synthetic-env-jina-0004"
    assert await provider_keys.get_provider_status("jina") == {"source": "env", "preview": "synt…0004"}


@pytest.mark.asyncio
async def test_encrypted_empty_override_blocks_environment_until_explicit_clear(provider_database):
    # Break caught: using a decrypted empty value as permission for env fallback.
    await provider_keys.set_provider_key("exchange", "")

    stored = provider_database.rows["provider_key:exchange"]
    assert crypto.is_encrypted(stored)
    assert crypto.decrypt_api_key(stored) == ""
    assert await provider_keys.get_provider_key("exchange") == ""

    await provider_keys.clear_provider_key("exchange")
    assert await provider_keys.get_provider_key("exchange") == "synthetic-env-exchange-0002"


@pytest.mark.asyncio
async def test_unknown_provider_is_missing_and_rejects_set_and_clear(provider_database):
    # Break caught: accepting arbitrary provider names as writable settings keys.
    assert await provider_keys.get_provider_key("synthetic-unknown-provider") == ""
    assert await provider_keys.get_provider_status("synthetic-unknown-provider") == {
        "source": "missing",
        "preview": "—",
    }
    with pytest.raises(ValueError, match="Unsupported runtime key provider"):
        await provider_keys.set_provider_key("synthetic-unknown-provider", "synthetic-unused-key")
    with pytest.raises(ValueError, match="Unsupported runtime key provider"):
        await provider_keys.clear_provider_key("synthetic-unknown-provider")
    assert provider_database.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["image", "transcription"])
async def test_pollinations_unreadable_override_stops_before_http(
    provider_database, unreadable_override, monkeypatch, consumer
):
    # Break caught: a consumer uses settings directly or submits without a key.
    from app.providers import pollinations

    provider_database.rows["provider_key:pollinations"] = unreadable_override
    requests: list[httpx.Request] = []

    def reject_request(request):
        requests.append(request)
        return httpx.Response(401)

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(reject_request)
    monkeypatch.setattr(pollinations.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    provider = pollinations.PollinationsProvider()
    if consumer == "image":
        result = await provider._generate_one_model("synthetic image prompt", model="flux")
        assert result.success is False
        assert result.error_message == "unauthorized"
        assert result.model_used == "flux"
        assert result.images == []
    else:
        assert await provider._transcribe_one_model(b"synthetic-audio-payload") is None
    assert requests == []
