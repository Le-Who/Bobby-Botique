"""Transport accounting excludes lifecycle scopes and local admission failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import metrics
from app.metrics import MetricsCollector
from app.observability.workload_events import start_workload_attempt


@pytest.fixture
def collector(monkeypatch):
    value = MetricsCollector()
    monkeypatch.setattr(metrics, "metrics_collector", value)
    return value


def test_lifecycle_start_and_local_refusal_are_not_api_dispatches(collector):
    attempt = start_workload_attempt(workload="memory", provider="gemini", model="model", api_key="fake-key")
    attempt.finish(outcome="skipped", reason_code="quota_exhausted")
    collector._drain_events()
    assert collector.metrics.api_calls == {}


def test_explicit_dispatch_is_counted_once_per_attempt(collector):
    attempt = start_workload_attempt(workload="memory", provider="gemini", model="model", api_key="fake-key")
    attempt.record_dispatch()
    attempt.record_dispatch()
    collector._drain_events()
    assert collector.metrics.api_calls == {"gemini": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("quota_error", [False, True])
async def test_consolidation_fallback_refusal_does_not_invent_an_attempt(monkeypatch, collector, override, quota_error):
    from app.providers import gemini
    from app.repos import keys, memory_consolidation

    async def configured(process, models, execute, **kwargs):
        return await execute(models[0], "fake-key") if override else None

    reservations = [True, RuntimeError("synthetic quota unavailable") if quota_error else False]
    # The configured execution helper has already reserved the first attempt.
    if override:
        reservations.pop(0)
    monkeypatch.setattr(memory_consolidation, "run_gemini_override", configured)
    monkeypatch.setattr(keys, "reserve_gemini_key_usage", AsyncMock(side_effect=reservations))
    generate = AsyncMock(side_effect=ValueError("synthetic provider failure"))
    monkeypatch.setattr(
        gemini,
        "get_cached_genai_client",
        lambda key: SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))),
    )

    assert await memory_consolidation._extract_graph("synthetic memory", "fake-key") == {
        "facts": [],
        "entities": [],
        "relations": [],
    }
    collector._drain_events()
    assert collector.metrics.api_calls == {"gemini": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_elevenlabs_configuration_failure_has_no_transport_attempt(monkeypatch, collector, cancelled):
    from app.providers import elevenlabs_tts

    failure = asyncio.CancelledError if cancelled else ValueError
    monkeypatch.setattr(elevenlabs_tts, "get_config", AsyncMock(side_effect=failure))
    with pytest.raises(failure):
        await elevenlabs_tts.generate_speech_elevenlabs("Hello", "fake-key", voice_id="voice")
    collector._drain_events()
    assert collector.metrics.api_calls == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_image_sdk_does_not_hide_retry_attempts_below_accounting(monkeypatch, collector, status):
    from google import genai

    from app.providers import gemini, imagen_provider

    requests = []

    def respond(request):
        requests.append(request)
        code = "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE"
        return httpx.Response(status, json={"error": {"code": code, "message": "synthetic provider failure"}})

    original_client = genai.Client
    clients = []
    monkeypatch.setattr(imagen_provider.settings, "GEMINI_API_KEYS", ["fake-image-key"])
    monkeypatch.setattr(imagen_provider.settings, "IMAGE_GEN_MAX_RETRIES", 1)
    monkeypatch.setattr(imagen_provider, "_reserve_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr(gemini, "_gemini_clients_cache", {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:

        def client_factory(**kwargs):
            options = kwargs["http_options"].model_copy(update={"httpx_async_client": transport})
            client = original_client(api_key=kwargs["api_key"], http_options=options)
            clients.append(client)
            return client

        monkeypatch.setattr(gemini.genai, "Client", client_factory)
        try:
            result = await imagen_provider.ImagenProvider().generate("synthetic image")
        finally:
            for client in clients:
                await client.aio.aclose()
                client.close()
    collector._drain_events()
    assert result.success is False
    assert len(requests) == 1
    assert collector.metrics.api_calls == {"gemini": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["weather", "crypto", "exchange"])
async def test_keyless_direct_intent_counts_each_failed_transport(monkeypatch, collector, operation):
    from app import intent_router

    requests = []

    def respond(request):
        requests.append(request)
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json={"results": [{"name": "Kyiv", "latitude": 50.45, "longitude": 30.52}]})
        return httpx.Response(503, json={})

    monkeypatch.setattr(intent_router, "metrics_collector", collector, raising=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(intent_router, "_get_http", lambda: transport)
        if operation == "weather":
            result = await intent_router._fetch_open_meteo("Kyiv")
            expected_calls = {"open_meteo_geocoding": 1, "open_meteo": 1}
            expected_requests = 2
        elif operation == "crypto":
            result = await intent_router._handle_crypto("bitcoin", "bitcoin price")
            expected_calls, expected_requests = {"coingecko": 1}, 1
        else:
            result = await intent_router._fetch_frankfurter("EUR", "USD")
            expected_calls, expected_requests = {"frankfurter": 1}, 1
    collector._drain_events()
    assert result is None
    assert len(requests) == expected_requests
    assert collector.metrics.api_calls == expected_calls
