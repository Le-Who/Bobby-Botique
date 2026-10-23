"""Explicit SDK shutdown drains borrowed clients even after cache eviction."""

from types import SimpleNamespace
from weakref import WeakValueDictionary

from cachetools import LRUCache

from app.providers import gemini


class _Client:
    def __init__(self, **_kwargs):
        self.async_closed = False
        self.sync_closed = False
        self.aio = SimpleNamespace(aclose=self.aclose)

    async def aclose(self):
        self.async_closed = True

    def close(self):
        self.sync_closed = True


async def test_shutdown_closes_evicted_sdk_client_without_closing_it_during_use(monkeypatch):
    monkeypatch.setattr(gemini, "_gemini_clients_cache", LRUCache(maxsize=1))
    monkeypatch.setattr(gemini, "_gemini_clients_by_id", WeakValueDictionary(), raising=False)
    monkeypatch.setattr(gemini.genai, "Client", _Client)
    borrowed = gemini.get_cached_genai_client("synthetic-first-key")
    current = gemini.get_cached_genai_client("synthetic-second-key")

    assert borrowed.async_closed is False
    assert borrowed.sync_closed is False
    await gemini.close_cached_genai_clients()

    assert borrowed.async_closed is True
    assert borrowed.sync_closed is True
    assert current.async_closed is True
    assert current.sync_closed is True
    assert not gemini._gemini_clients_cache


async def test_shutdown_closes_and_resets_both_vertex_singletons(monkeypatch):
    express, live = _Client(), _Client()
    monkeypatch.setattr(gemini, "_gemini_clients_cache", {})
    monkeypatch.setattr(gemini, "_gemini_clients_by_id", WeakValueDictionary(), raising=False)
    monkeypatch.setattr(gemini, "_vertex_client", express)
    monkeypatch.setattr(gemini, "_vertex_live_client", live)
    monkeypatch.setattr(gemini, "_vertex_client_initialized", True)
    monkeypatch.setattr(gemini, "_vertex_live_client_initialized", True)
    monkeypatch.setattr(gemini, "_vertex_disabled_until_monotonic", 123.0)

    await gemini.close_cached_genai_clients()

    assert express.async_closed is True and express.sync_closed is True
    assert live.async_closed is True and live.sync_closed is True
    assert gemini._vertex_client is None
    assert gemini._vertex_live_client is None
    assert gemini._vertex_client_initialized is False
    assert gemini._vertex_live_client_initialized is False
    assert gemini._vertex_disabled_until_monotonic == 0.0
