"""Regression tests for non-LLM external API integrations."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100 евро в доллары", ("EUR", "USD")),
        ("convert EUR to USD", ("EUR", "USD")),
        ("курс USD к EUR", ("USD", "EUR")),
        ("convert CHF to TRY", ("CHF", "TRY")),
    ],
)
def test_currency_pair_follows_mention_order(text, expected):
    from app.intent_router import _extract_currency_pair

    assert _extract_currency_pair(text) == expected


@pytest.mark.parametrize("text", ["country report", "сумма платежа"])
def test_currency_pair_does_not_match_alias_inside_another_word(text):
    from app.intent_router import _extract_currency_pair

    assert _extract_currency_pair(text) == (None, None)


@pytest.mark.asyncio
async def test_fiat_conversion_preserves_requested_amount(monkeypatch):
    from app import intent_router
    from app.repos import provider_keys

    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value="exchange-key"))
    fetch = AsyncMock(return_value=intent_router.IntentResult("converted"))
    monkeypatch.setattr(intent_router, "_fetch_exchangerate_api", fetch)

    result = await intent_router._handle_fiat_currency("100 евро в доллары")

    assert result is not None
    fetch.assert_awaited_once_with("exchange-key", "EUR", "USD", 100.0)


@pytest.mark.asyncio
async def test_provider_key_is_encrypted_at_rest_and_decrypted_at_runtime(monkeypatch):
    from app.repos import provider_keys

    secret = "weather-secret-value"
    stored: dict[str, str] = {}

    async def save(key: str, value: str) -> None:
        stored[key] = value

    async def load(key: str, default: str = "") -> str:
        return stored.get(key, default)

    monkeypatch.setattr(provider_keys, "set_global_setting", save)
    monkeypatch.setattr(provider_keys, "get_global_setting", load)
    monkeypatch.setattr(provider_keys, "encrypt_api_key", lambda value: f"encrypted:{value[::-1]}", raising=False)
    monkeypatch.setattr(
        provider_keys,
        "safe_decrypt",
        lambda value: value.removeprefix("encrypted:")[::-1],
        raising=False,
    )
    monkeypatch.setattr(provider_keys, "is_encrypted", lambda value: value.startswith("encrypted:"), raising=False)

    await provider_keys.set_provider_key("weather", secret)

    assert stored["provider_key:weather"] != secret
    assert secret not in stored["provider_key:weather"]
    assert await provider_keys.get_provider_key("weather") == secret


@pytest.mark.asyncio
async def test_legacy_plaintext_provider_key_is_migrated_on_read(monkeypatch):
    from app.repos import provider_keys

    stored = {"provider_key:weather": "legacy-plaintext-secret"}
    writes: list[tuple[str, str]] = []

    async def load(key: str, default: str = "") -> str:
        return stored.get(key, default)

    async def save(key: str, value: str) -> None:
        writes.append((key, value))
        stored[key] = value

    monkeypatch.setattr(provider_keys, "get_global_setting", load)
    monkeypatch.setattr(provider_keys, "set_global_setting", save)
    monkeypatch.setattr(provider_keys, "is_encrypted", lambda value: value.startswith("encrypted:"), raising=False)
    monkeypatch.setattr(provider_keys, "safe_decrypt", lambda value: value, raising=False)
    monkeypatch.setattr(provider_keys, "encrypt_api_key", lambda value: f"encrypted:{value[::-1]}", raising=False)

    assert await provider_keys.get_provider_key("weather") == "legacy-plaintext-secret"
    assert writes == [("provider_key:weather", "encrypted:terces-txetnialp-ycagel")]


@pytest.mark.asyncio
async def test_global_setting_write_log_never_contains_value(monkeypatch, caplog):
    from app.repos import settings_repo

    monkeypatch.setattr(settings_repo, "_ensure_table", AsyncMock())
    monkeypatch.setattr(settings_repo.db, "db_query", AsyncMock(return_value=[]))
    secret = "do-not-log-this-secret"

    with caplog.at_level(logging.INFO):
        await settings_repo.set_global_setting("provider_key:weather", secret)

    assert secret not in caplog.text


def test_webhook_path_is_stable_and_does_not_contain_bot_token():
    from bot import _telegram_webhook_path

    token = "123456:super-secret-bot-token"
    first = _telegram_webhook_path(token)

    assert first == _telegram_webhook_path(token)
    assert first.startswith("/webhook/")
    assert token not in first
    assert "super-secret" not in first


def test_bot_does_not_schedule_quota_consuming_provider_probes():
    import inspect

    import bot

    assert 'name="provider_health_check"' not in inspect.getsource(bot)


def test_runtime_key_wizard_lists_only_resolved_non_llm_providers():
    from app.handlers.cmd_keys import _PROVIDERS

    assert set(_PROVIDERS) == {"weather", "exchange", "pollinations", "jina"}


@pytest.mark.asyncio
async def test_provider_key_callback_rechecks_admin_permission(monkeypatch):
    from app.handlers import cmd_keys
    from app.utils import decorators

    query = AsyncMock()
    query.id = "callback-id"
    query.data = "keys:clear:weather"
    update = MagicMock()
    update.effective_user.id = 999
    update.effective_chat.id = 123
    update.callback_query = query
    update.message = None
    context = MagicMock()
    context.user_data = {}
    clear = AsyncMock()
    monkeypatch.setattr(decorators, "is_admin", lambda _user_id: False)
    monkeypatch.setattr(cmd_keys, "clear_provider_key", clear)

    await cmd_keys.keys_callback(update, context)

    clear.assert_not_awaited()
    query.answer.assert_awaited_once_with("❌ У вас нет прав администратора.", show_alert=True)


@pytest.mark.asyncio
async def test_pollinations_uses_runtime_provider_key_override(monkeypatch):
    from app.providers import pollinations
    from app.repos import provider_keys

    key_lookup = AsyncMock(return_value="runtime-pollinations-key")
    monkeypatch.setattr(provider_keys, "get_provider_key", key_lookup)
    monkeypatch.setattr(pollinations.settings, "POLLINATIONS_API_KEY", "env-key")
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": [{"b64_json": "aGVsbG8="}]}
    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(pollinations.httpx, "AsyncClient", lambda **kwargs: client)

    result = await pollinations.PollinationsProvider()._try_post(
        prompt="cat",
        model="flux",
        width=512,
        height=512,
        seed=1,
        enhance=False,
        negative_prompt="",
        timeout=5,
    )

    assert result.success is True
    assert client.post.await_args.kwargs["headers"]["Authorization"] == "Bearer runtime-pollinations-key"
    key_lookup.assert_awaited_once_with("pollinations")


@pytest.mark.asyncio
async def test_jina_search_uses_runtime_provider_key_override(monkeypatch):
    from app import search_jina
    from app.repos import provider_keys

    key_lookup = AsyncMock(return_value="runtime-jina-key")
    monkeypatch.setattr(provider_keys, "get_provider_key", key_lookup)
    response = MagicMock()
    response.text = "result"
    response.raise_for_status.return_value = None
    client = AsyncMock()
    client.get.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(search_jina.httpx, "AsyncClient", lambda **kwargs: client)

    result = await search_jina.search_jina("query")

    assert result.content == "result"
    assert client.get.await_args.kwargs["headers"]["Authorization"] == "Bearer runtime-jina-key"
    key_lookup.assert_awaited_once_with("jina")


@pytest.mark.asyncio
async def test_imagen_user_daily_quota_is_bounded_without_redis(monkeypatch):
    from app.providers import imagen_provider

    monkeypatch.setattr(imagen_provider.settings, "IMAGE_GEN_DAILY_LIMIT", 1)
    monkeypatch.setattr(
        imagen_provider,
        "_redis_consume_user_quota",
        AsyncMock(return_value=None),
        raising=False,
    )
    monkeypatch.setattr(imagen_provider, "_USER_DAY_BUCKET", {}, raising=False)

    assert await imagen_provider._consume_user_daily_quota(42) is True
    assert await imagen_provider._consume_user_daily_quota(42) is False


@pytest.mark.asyncio
async def test_imagen_rejects_before_provider_call_when_user_quota_exhausted(monkeypatch):
    from app.providers import imagen_provider

    quota = AsyncMock(return_value=False)
    monkeypatch.setattr(imagen_provider, "_consume_user_daily_quota", quota, raising=False)

    result = await imagen_provider.ImagenProvider().generate(
        "cat",
        user_id=42,
    )

    assert result.success is False
    assert result.error_message == "user_daily_limit"
    quota.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_weather_error_log_does_not_expose_api_key(monkeypatch, caplog):
    from app import intent_router

    secret = "weather-key-in-request-url"
    client = AsyncMock()
    client.get.side_effect = RuntimeError(f"GET https://weather.test/?key={secret}")
    monkeypatch.setattr(intent_router, "_get_http", lambda: client)

    with caplog.at_level(logging.WARNING):
        assert await intent_router._fetch_weatherapi(secret, "Kyiv") is None

    assert secret not in caplog.text
    assert "Kyiv" not in caplog.text


@pytest.mark.asyncio
async def test_exchange_error_log_does_not_expose_api_key(monkeypatch, caplog):
    from app import intent_router

    secret = "exchange-key-in-path"
    client = AsyncMock()
    client.get.side_effect = RuntimeError(f"GET https://exchange.test/v6/{secret}/pair/USD/EUR")
    monkeypatch.setattr(intent_router, "_get_http", lambda: client)

    with caplog.at_level(logging.WARNING):
        assert await intent_router._fetch_exchangerate_api(secret, "USD", "EUR") is None

    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_fta_image_log_does_not_include_prompt_or_base64(monkeypatch, caplog):
    from app.providers import freetheai_image

    prompt = "private portrait prompt"
    image_base64 = "very-sensitive-base64-payload"
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": [{"b64_json": "aGVsbG8="}]}
    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(freetheai_image, "_pick_key", lambda: ("api-key", "hash"))
    monkeypatch.setattr(freetheai_image.httpx, "AsyncClient", lambda **kwargs: client)

    with caplog.at_level(logging.INFO):
        result = await freetheai_image.FreeTheAIImageProvider().generate(
            prompt,
            image_base64=image_base64,
        )

    assert result.success is True
    assert prompt not in caplog.text
    assert image_base64 not in caplog.text


def test_anonymous_x0_upload_helper_is_not_shipped():
    from app import document_processor

    assert not hasattr(document_processor, "upload_to_x0_at")


def test_obsolete_api_ninjas_horoscope_seed_is_not_shipped():
    from pathlib import Path

    assert not Path("seed_horoscope.py").exists()


def test_local_telegram_file_path_cannot_escape_shared_volume():
    from app.utils.tg_file import _extract_local_path

    assert _extract_local_path("/etc/passwd") is None
    assert _extract_local_path("/var/lib/telegram-bot-api/../../etc/passwd") is None
    assert _extract_local_path("/var/lib/telegram-bot-api/bot/file.ogg") is not None


@pytest.mark.asyncio
async def test_cloud_telegram_download_rejects_oversized_file():
    from app.utils.tg_file import MAX_TELEGRAM_FILE_BYTES, get_file_bytes

    bot = MagicMock()
    bot.local_mode = False
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(MAX_TELEGRAM_FILE_BYTES + 1))

    with pytest.raises(ValueError, match="too large"):
        await get_file_bytes(bot, tg_file)


def test_redis_tls_verification_is_secure_by_default(monkeypatch):
    from app import cache

    monkeypatch.delenv("REDIS_TLS_VERIFY", raising=False)

    assert cache._redis_tls_options("rediss://cache.example.com:6379") == {
        "ssl_cert_reqs": "required",
        "ssl_check_hostname": True,
    }
    assert cache._redis_tls_options("redis://localhost:6379") == {}


@pytest.mark.asyncio
async def test_tavily_cache_log_does_not_include_user_query(monkeypatch, caplog):
    from app import search_services

    private_query = "private-search-query-7f31"
    monkeypatch.setattr(
        search_services,
        "get_cached_search_result",
        AsyncMock(return_value={"type": "search", "results": []}),
    )

    with caplog.at_level(logging.INFO):
        result = await search_services.tavily_search_agent(private_query)

    assert result == {"type": "search", "results": []}
    assert private_query not in caplog.text


@pytest.mark.asyncio
async def test_tavily_http_error_does_not_log_upstream_body_or_query(monkeypatch, caplog):
    from app import search_services

    private_query = "private-search-query-a81c"
    upstream_secret = "upstream-body-secret-b411"
    request = httpx.Request("POST", "https://api.tavily.com/search")
    response = httpx.Response(500, text=upstream_secret, request=request)
    error = httpx.HTTPStatusError("failed", request=request, response=response)

    monkeypatch.setattr(search_services, "get_cached_search_result", AsyncMock(return_value=None))
    monkeypatch.setattr(
        search_services,
        "get_available_tavily_key",
        AsyncMock(return_value={"api_key": "key", "key_hash": "hash"}),
    )
    monkeypatch.setattr(search_services, "_tavily_api_call", AsyncMock(side_effect=error))
    monkeypatch.setattr(search_services.metrics_collector, "record_search_query", AsyncMock())
    monkeypatch.setattr(search_services.metrics_collector, "record_api_call", AsyncMock())
    monkeypatch.setattr(search_services.metrics_collector, "record_error", AsyncMock())

    with caplog.at_level(logging.INFO):
        result = await search_services.tavily_search_agent(private_query)

    assert result["error"].startswith("Ошибка API поиска: 500")
    assert private_query not in caplog.text
    assert upstream_secret not in caplog.text


@pytest.mark.asyncio
async def test_jina_failure_log_does_not_include_query_or_raw_exception(monkeypatch, caplog):
    from app import search_jina
    from app.repos import provider_keys

    private_query = "private-jina-query-c4de"
    upstream_secret = "jina-transport-secret-98ad"
    client = AsyncMock()
    client.get.side_effect = RuntimeError(upstream_secret)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value="jina-key"))
    monkeypatch.setattr(search_jina.httpx, "AsyncClient", lambda **kwargs: client)

    with caplog.at_level(logging.INFO):
        result = await search_jina.search_jina(private_query)

    assert result.content == ""
    assert private_query not in caplog.text
    assert upstream_secret not in caplog.text


@pytest.mark.asyncio
async def test_fta_error_logs_do_not_include_upstream_response_body(monkeypatch, caplog):
    from app.providers import freetheai_audio, freetheai_image

    upstream_secret = "media-upstream-body-secret-6ba2"
    response = MagicMock(status_code=500, text=upstream_secret)
    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(freetheai_image, "_pick_key", lambda: ("api-key", "hash"))
    monkeypatch.setattr(freetheai_audio, "_pick_key", lambda: ("api-key", "hash"))
    monkeypatch.setattr(freetheai_image.httpx, "AsyncClient", lambda **kwargs: client)

    with caplog.at_level(logging.INFO):
        image_result = await freetheai_image.FreeTheAIImageProvider().generate("private prompt")

    monkeypatch.setattr(freetheai_audio.httpx, "AsyncClient", lambda **kwargs: client)
    with caplog.at_level(logging.INFO):
        audio_result = await freetheai_audio.FreeTheAIAudioProvider().generate("private prompt")

    assert image_result.error_message == "http_500"
    assert audio_result.error_message == "http_500"
    assert upstream_secret not in caplog.text


@pytest.mark.asyncio
async def test_pollinations_transcription_error_log_does_not_include_upstream_body(monkeypatch, caplog):
    from app.providers import pollinations
    from app.repos import provider_keys

    upstream_secret = "pollinations-upstream-secret-91a0"
    response = MagicMock(status_code=500, text=upstream_secret)
    client = AsyncMock()
    client.post.return_value = response
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value="runtime-key"))
    monkeypatch.setattr(pollinations.httpx, "AsyncClient", lambda **kwargs: client)

    with caplog.at_level(logging.INFO):
        result = await pollinations.PollinationsProvider().transcribe_audio(b"audio")

    assert result is None
    assert upstream_secret not in caplog.text


@pytest.mark.asyncio
async def test_imagen_error_is_sanitized_in_logs_and_user_result(monkeypatch, caplog):
    from app.providers import imagen_provider

    upstream_secret = "imagen-upstream-secret-357c"
    client = MagicMock()
    client.aio.models.generate_images = AsyncMock(side_effect=RuntimeError(upstream_secret))
    monkeypatch.setattr(imagen_provider.settings, "GEMINI_API_KEYS", ["imagen-api-key-raw"])
    monkeypatch.setattr(imagen_provider.settings, "IMAGE_GEN_MAX_RETRIES", 1)
    monkeypatch.setattr(imagen_provider, "_consume_user_daily_quota", AsyncMock(return_value=True))
    monkeypatch.setattr(imagen_provider, "_get_key_usage", AsyncMock(return_value=0))
    monkeypatch.setattr(imagen_provider, "get_cached_genai_client", lambda _key: client)

    with caplog.at_level(logging.INFO):
        result = await imagen_provider.ImagenProvider().generate("private prompt", user_id=42)

    assert result.success is False
    assert result.error_message == "unexpected:RuntimeError"
    assert upstream_secret not in caplog.text
    assert "-raw" not in caplog.text
