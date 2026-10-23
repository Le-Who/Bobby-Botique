"""Provider pools are drained after speech jobs without leaving peers open."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_cached_gemini_clients_close_both_transports_and_clear_cache(monkeypatch):
    from app.providers import gemini

    clients = [SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock()) for _ in range(2)]
    clients[0].aio.aclose.side_effect = RuntimeError("offline close failure")
    cache = {str(index): client for index, client in enumerate(clients)}
    monkeypatch.setattr(gemini, "_gemini_clients_cache", cache)
    await gemini.close_cached_genai_clients()
    assert not cache
    for client in clients:
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()
    await gemini.close_cached_genai_clients()
    for client in clients:
        client.aio.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_shutdown_drains_all_provider_pools_even_if_one_close_fails(monkeypatch):
    import app.providers as providers

    close = [AsyncMock(side_effect=RuntimeError("offline failure")), AsyncMock(), AsyncMock()]
    monkeypatch.setattr(providers, "_close_openrouter_http_clients", close[0])
    monkeypatch.setattr(providers, "close_cached_genai_clients", close[1])
    monkeypatch.setattr(providers, "close_elevenlabs_client", close[2])
    await providers.close_http_clients()
    for operation in close:
        operation.assert_awaited_once()
