from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repos import memory


@pytest.fixture(autouse=True)
def isolate_expansion_policy(monkeypatch):
    """Timeout tests do not include settings-store or prompt-registry cold starts."""
    monkeypatch.setattr(memory, "run_gemini_override", AsyncMock(return_value=None))
    monkeypatch.setattr(memory, "get_prompt_text", lambda _: "Expand: {query}")


@pytest.mark.asyncio
async def test_optional_memory_query_expansion_times_out_and_keeps_original_query(monkeypatch) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_generate_content(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cancelled.set()

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=slow_generate_content)))
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda key: client)
    monkeypatch.setattr(memory, "QUERY_EXPANSION_TIMEOUT_SECONDS", 0.2)
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)

    task = asyncio.create_task(memory.expand_query_with_llm("Что мы обсуждали вчера?", "test-key"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        result = await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result == "Что мы обсуждали вчера?"
    assert cancelled.is_set()
    reserve.assert_awaited_once_with(hashlib.sha256(b"test-key").hexdigest(), memory.QUERY_EXPANSION_MODEL)


@pytest.mark.asyncio
async def test_expansion_policy_timeout_does_not_start_quota_or_sdk(monkeypatch):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def stalled_policy(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(memory, "run_gemini_override", stalled_policy)
    monkeypatch.setattr(memory, "QUERY_EXPANSION_TIMEOUT_SECONDS", 0.2)
    client = MagicMock()
    monkeypatch.setattr(memory, "get_cached_genai_client", client)
    reserve = AsyncMock()
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    task = asyncio.create_task(memory.expand_query_with_llm("original query", "test-key"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert await asyncio.wait_for(task, 2) == "original query"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set()
    reserve.assert_not_awaited()
    client.assert_not_called()
