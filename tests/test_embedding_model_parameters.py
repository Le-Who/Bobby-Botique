"""Embedding request fields stay compatible without changing the vector profile."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.genai import types


@pytest.mark.parametrize(
    "model",
    ["gemini-embedding-2-preview", "gemini-embedding-2", "models/gemini-embedding-2-preview", "gemini-embedding-001"],
)
@pytest.mark.parametrize("task", ["RETRIEVAL_QUERY", "RETRIEVAL_DOCUMENT"])
async def test_embedding_sdk_wire_uses_only_supported_task_parameters(monkeypatch, model, task):
    from app.repos import memory

    requests = []
    vector = [0.1] * 768

    def respond(request):
        requests.append(json.loads(request.content))
        body = (
            {"embeddings": [{"values": vector}]}
            if "batchEmbedContents" in request.url.path
            else {"embedding": {"values": vector}}
        )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = genai.Client(api_key="offline-key", http_options=types.HttpOptions(httpx_async_client=transport))
        monkeypatch.setattr(memory, "EMBEDDING_MODEL", model)
        monkeypatch.setattr(memory, "get_cached_genai_client", lambda _: client)
        monkeypatch.setattr("app.repos.keys.reserve_gemini_minute_usage", AsyncMock(return_value=True))
        try:
            result = await memory._get_embedding("synthetic unmodified input", "offline-key", task_type=task)
        finally:
            await client.aio.aclose()
            client.close()

    assert result == vector
    payload = requests[0].get("requests", [requests[0]])[0]
    assert payload["outputDimensionality"] == 768
    assert payload["content"]["parts"] == [{"text": "synthetic unmodified input"}]
    if "embedding-2" in model:
        assert "taskType" not in payload
    else:
        assert payload["taskType"] == task
