"""Real Gemini SDK/SSE parsing with delayed HTTP response headers."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai

from app.providers import gemini
from app.providers.router import ProviderRouter
from app.providers.stream_types import (
    GenerationRequest,
    PromptRole,
    PromptTurn,
    StreamCompleted,
    TextDelta,
    TextPart,
)

PRIMARY = "gemini-3.5-flash-lite"
BACKUP = "gemini-3.1-flash-lite"
SSE = b'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"ready"}]},"finishReason":"STOP"}]}\n\n'


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_stalls", [False, True])
async def test_sequential_chat_preserves_each_models_first_text_budget(monkeypatch, primary_stalls):
    # 0.2 s models a response arriving after the old half-budget (0.15 s)
    # but within the model's own 0.3 s allowance.
    original_budget = GenerationRequest.first_text_timeout_seconds
    monkeypatch.setattr(
        GenerationRequest,
        "first_text_timeout_seconds",
        property(lambda self: None if original_budget.__get__(self) is None else 0.3),
    )
    monkeypatch.setattr("app.providers.router.KEY_RACE_HEDGE_DELAY_SECONDS", 5.0)
    monkeypatch.setattr(gemini, "_gemini_clients_cache", {})
    monkeypatch.setattr(gemini, "is_vertex_client_available", lambda: False)
    calls = []
    cancelled_primary = []

    class Pool:
        async def resolve_exact_ai_request(self, model, *, excluded_key_hashes):
            for index in range(2):
                key = f"synthetic-{model}-{index}"
                if key not in excluded_key_hashes:
                    return {"api_key": key, "key_hash": key}, model, None
            return None, None, "all_exhausted"

        async def reserve_key_usage(self, *args):
            return True

    async def respond(request):
        model = PRIMARY if PRIMARY in request.url.path else BACKUP
        calls.append(model)
        if model == BACKUP:
            assert primary_stalls, "A primary within its budget must remain eligible"
            assert len(cancelled_primary) == 2
        try:
            if model == PRIMARY and primary_stalls:
                await asyncio.sleep(0.3)
                raise httpx.ReadTimeout("synthetic transport timeout")
            await asyncio.sleep(0.2)
            return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        except asyncio.CancelledError:
            if model == PRIMARY and not primary_stalls:
                cancelled_primary.append(model)
            raise
        finally:
            if model == PRIMARY and primary_stalls:
                cancelled_primary.append(model)

    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: Pool())
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: AsyncMock())
    real_client = genai.Client
    clients = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:

        def client(**kwargs):
            kwargs["http_options"].httpx_async_client = transport
            result = real_client(**kwargs)
            clients.append(result)
            return result

        monkeypatch.setattr(genai, "Client", client)
        request = GenerationRequest(
            models=(PRIMARY, BACKUP),
            turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic question"),)),),
            route_strategy="sequential",
            allow_model_fallback=False,
            allow_deferred=False,
        )
        try:
            # Measure delayed HTTP headers independently of cold SDK setup.
            for model in (PRIMARY, BACKUP):
                for index in range(2):
                    gemini.get_cached_genai_client(f"synthetic-{model}-{index}")
            events = [event async for event in ProviderRouter().stream(request)]
        finally:
            for sdk_client in clients:
                await sdk_client.aio.aclose()
                sdk_client.close()

    assert events[0] == TextDelta("ready")
    assert len(events) == 2 and isinstance(events[-1], StreamCompleted)
    assert events[-1].route.actual_model == (BACKUP if primary_stalls else PRIMARY)
    assert calls == [PRIMARY, PRIMARY] + ([BACKUP] if primary_stalls else [])
