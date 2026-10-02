"""Direct Gemini memory callers retain their structured SDK options under policy overrides."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _client(text: str):
    generate = AsyncMock(return_value=SimpleNamespace(text=text))
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate))), generate


async def _chosen_model(process_id, baseline, execute, *, initial_api_key=None):
    assert baseline and initial_api_key == "test-key"
    return await execute("gemini-3.8-flash", "test-key")


@pytest.mark.asyncio
async def test_structured_memory_extraction_keeps_schema_and_thinking_config(monkeypatch):
    from app.repos import memory_extraction as extraction

    client, generate = _client('{"entities":[],"relations":[]}')
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(extraction, "run_gemini_override", _chosen_model, raising=False)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(record_success=AsyncMock()))
    result = await extraction.extract_graph_structured("Alice works on a project", "test-key")
    assert result.entities == []
    assert generate.await_args.kwargs["model"] == "gemini-3.8-flash"
    config = generate.await_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema
    assert config.max_output_tokens == 2048


@pytest.mark.asyncio
async def test_memory_consolidation_keeps_json_config_and_source_ids(monkeypatch):
    from app.repos import memory_consolidation as consolidation

    raw = '{"facts":[{"text":"Alice codes","source_ids":[17]}],"entities":[],"relations":[]}'
    client, generate = _client(raw)
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(consolidation, "run_gemini_override", _chosen_model, raising=False)
    result = await consolidation._extract_graph("memory_id=17: Alice codes", "test-key")
    assert result["facts"][0]["source_ids"] == [17]
    assert generate.await_args.kwargs["model"] == "gemini-3.8-flash"
    config = generate.await_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.max_output_tokens == 2048


@pytest.mark.asyncio
async def test_memory_query_expansion_keeps_short_output_budget(monkeypatch):
    from app.repos import memory

    client, generate = _client("Python project")
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(memory, "run_gemini_override", _chosen_model, raising=False)
    expanded = await memory.expand_query_with_llm("That coding project?", "test-key")
    assert expanded == "Python project"
    assert generate.await_args.kwargs["model"] == "gemini-3.8-flash"
    assert generate.await_args.kwargs["config"].max_output_tokens == 60


@pytest.mark.asyncio
async def test_memory_taxonomy_judge_keeps_safe_default_and_small_budget(monkeypatch):
    from app.repos import memory_extraction as extraction

    client, generate = _client("parallel")
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(extraction, "run_gemini_override", _chosen_model, raising=False)
    verdict = await extraction._resolve_ambiguous_conflict("likes Python", "likes Rust", "Alice", "code", "test-key")
    assert verdict == "parallel"
    assert generate.await_args.kwargs["model"] == "gemini-3.8-flash"
    assert generate.await_args.kwargs["config"].max_output_tokens == 10


@pytest.mark.asyncio
async def test_memory_relevance_judge_uses_configured_model_without_losing_json_mode(monkeypatch):
    from app.repos import memory

    client, generate = _client('[{"index":0,"relevant":true}]')
    monkeypatch.setattr(memory, "get_cached_genai_client", lambda _: client)
    monkeypatch.setattr(memory, "search_memories", AsyncMock(return_value=[{"content": "Alice codes", "id": 17}]))
    monkeypatch.setattr(memory, "_is_ltm_read_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(memory, "run_gemini_override", _chosen_model, raising=False)
    result = await memory._search_memories_with_llm_judge_impl(42, "What does Alice do?", "test-key")
    assert result[0]["id"] == 17
    assert result[0]["llm_judged"] is True
    assert generate.await_args.kwargs["model"] == "gemini-3.8-flash"
    config = generate.await_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.max_output_tokens == 256
