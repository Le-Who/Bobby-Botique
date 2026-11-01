"""Internal workloads reserve each dispatch and recover without poisoning keys."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai.errors import APIError

from app.process_policies import ResolvedPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("error", [APIError(429, {"error": {"message": "Quota exceeded"}}), httpx.ReadTimeout("slow")])
async def test_consolidation_rotates_provider_failure_instead_of_retrying_plain_text(monkeypatch, explicit, error):
    from app.repos import memory_consolidation as consolidation
    from app.runtime_settings import gemini_execution

    model = "gemini-test"
    monkeypatch.setattr(consolidation, "_CONSOLIDATION_MODEL", model)
    monkeypatch.setattr(
        gemini_execution, "resolve_process", AsyncMock(return_value=ResolvedPolicy((model,), "sequential", explicit))
    )
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "healthy", "key_hash": "healthy-hash"}),
    )
    reservation = AsyncMock(return_value=True)
    health = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reservation)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    attempts = []

    def client(key):
        async def generate(**kwargs):
            attempts.append((key, kwargs["model"], kwargs["config"].response_mime_type))
            if key == "busy":
                raise error
            return SimpleNamespace(text='{"facts":["checked"],"entities":[],"relations":[]}')

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", client)
    assert await consolidation._extract_graph("synthetic", "busy") == {
        "facts": ["checked"],
        "entities": [],
        "relations": [],
    }
    assert attempts == [("busy", model, "application/json"), ("healthy", model, "application/json")]
    assert reservation.await_count == 2
    assert health.suspend_key.await_count == 1
    health.record_success.assert_awaited_once_with("healthy-hash", model)


@pytest.mark.asyncio
async def test_memory_transport_failure_rotates_under_one_policy_budget(monkeypatch):
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-test",), "sequential", True)),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "healthy", "key_hash": "healthy-hash"}),
    )
    monkeypatch.setattr(
        "app.repos.keys.get_key_status_manager",
        lambda: SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock()),
    )
    attempts = []

    async def execute(model, key):
        attempts.append(key)
        if key == "busy":
            raise httpx.ConnectError("synthetic network failure")
        return "checked"

    assert (
        await gemini_execution.run_gemini_override("memory.extract", (), execute, initial_api_key="busy") == "checked"
    )
    assert attempts == ["busy", "healthy"]


@pytest.mark.asyncio
async def test_daily_text_success_restores_key_health(monkeypatch):
    from app.games.daily_ai import generate_daily_text

    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key",
        AsyncMock(return_value={"api_key": "healthy", "key_hash": "healthy-hash"}),
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    health = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content=AsyncMock(return_value=SimpleNamespace(text='{"ok":true}')))
        )
    )
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    assert await generate_daily_text("synthetic", "gemini-test") == '{"ok":true}'
    health.record_success.assert_awaited_once_with("healthy-hash", "gemini-test")
    health.suspend_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_embedding_rotation_excludes_the_full_stored_key_hash(monkeypatch):
    from app.repos import memory

    bad_hash = hashlib.sha256(b"bad-key").hexdigest()
    attempts = []

    async def select(model, **kwargs):
        key = "healthy" if bad_hash in kwargs.get("excluded_key_hashes", ()) else "bad-key"
        return {"api_key": key, "key_hash": hashlib.sha256(key.encode()).hexdigest()}, model, None

    def client(key):
        async def embed(**kwargs):
            attempts.append(key)
            if key == "bad-key":
                raise APIError(400, {"error": {"message": "API_KEY_INVALID"}})
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.25, 0.5])])

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed)))

    monkeypatch.setattr("app.handlers.ai_core._resolve_ai_request", select)
    monkeypatch.setattr(memory, "get_cached_genai_client", client)
    assert await memory._get_embedding("synthetic", "bad-key") == [0.25, 0.5]
    assert attempts == ["bad-key", "healthy"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        '{"facts":null,"entities":[],"relations":[]}',
        '{"facts":[],"entities":{},"relations":[]}',
        '{"facts":[],"entities":[],"relations":[null]}',
        '{"facts":[],"entities":[],"relations":["invalid"]}',
    ],
)
async def test_consolidation_invalid_structure_uses_reserved_format_fallback(monkeypatch, raw):
    from app.repos import memory_consolidation as consolidation
    from app.runtime_settings import gemini_execution

    monkeypatch.setattr(
        gemini_execution,
        "resolve_process",
        AsyncMock(return_value=ResolvedPolicy(("gemini-test",), "sequential", False)),
    )
    reservations = AsyncMock(return_value=True)
    health = SimpleNamespace(record_success=AsyncMock(), suspend_key=AsyncMock())
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reservations)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: health)
    generate = AsyncMock(side_effect=[SimpleNamespace(text=raw), SimpleNamespace(text="- checked fact")])
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    assert await consolidation._extract_graph("synthetic", "fake-key") == {
        "facts": ["checked fact"],
        "entities": [],
        "relations": [],
    }
    assert reservations.await_count == generate.await_count == 2
    health.suspend_key.assert_not_awaited()
