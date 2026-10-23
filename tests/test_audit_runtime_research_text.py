"""Agentic direct answers preserve all visible SDK response parts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.core import agentic


@pytest.mark.parametrize("leading_thought", [False, True])
async def test_direct_research_answer_joins_all_visible_text_parts(monkeypatch, leading_thought):
    parts = [types.Part(text="The answer is "), types.Part(text="forty-two.")]
    if leading_thought:
        parts.insert(0, types.Part(text="Synthetic reasoning summary", thought=True))
    response = types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=12),
    )
    generate = AsyncMock(return_value=response)
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(agentic, "get_cached_genai_client", lambda _key: client)
    search = agentic.AgenticSearch("gemini-3.5-flash", "synthetic-key")

    result = await search.run("A synthetic question", AsyncMock())

    assert result.answer == "The answer is forty-two."
    assert result.llm_calls == 1
    assert result.total_tokens == 12
