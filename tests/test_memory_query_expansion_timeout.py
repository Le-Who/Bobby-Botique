from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.repos import memory


@pytest.mark.asyncio
async def test_optional_memory_query_expansion_times_out_and_keeps_original_query(monkeypatch) -> None:
    cancelled = asyncio.Event()

    async def slow_generate_content(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=slow_generate_content)))
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda key: client)
    monkeypatch.setattr(memory, "QUERY_EXPANSION_TIMEOUT_SECONDS", 0.01, raising=False)

    result = await asyncio.wait_for(memory.expand_query_with_llm("Что мы обсуждали вчера?", "test-key"), 0.25)

    assert result == "Что мы обсуждали вчера?"
    assert cancelled.is_set()
