from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers import pollinations
from app.repos import provider_keys


@pytest.mark.asyncio
async def test_missing_key_stops_before_generation(monkeypatch):
    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value=""))
    post = AsyncMock(return_value=pollinations.PollinationsResult(success=True))
    monkeypatch.setattr(pollinations.PollinationsProvider, "_try_post", post)
    result = await pollinations.PollinationsProvider().generate("cat")
    assert result.error_message == "unauthorized"
    post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["unauthorized", "paid_tier_required", "timeout", "http_429"])
async def test_generation_failure_never_retries_anonymously_or_changes_request(monkeypatch, error):
    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value="test-key"))
    post = AsyncMock(return_value=pollinations.PollinationsResult(success=False, error_message=error))
    get = AsyncMock(return_value=pollinations.PollinationsResult(success=True))
    monkeypatch.setattr(pollinations.PollinationsProvider, "_try_post", post)
    monkeypatch.setattr(pollinations.PollinationsProvider, "_try_get", get)
    result = await pollinations.PollinationsProvider().generate("cat", model="qwen/qwen-image-3")
    assert result.error_message == error
    assert post.await_args.kwargs["model"] == "qwen/qwen-image-3"
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_preserves_aliases_and_filters_video(monkeypatch):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "name": "qwen/qwen-image",
                    "aliases": ["qwen-image"],
                    "publisher": "Qwen",
                    "output_modalities": ["image"],
                    "supported_endpoints": ["/v1/images/generations"],
                },
                {"name": "video/model", "output_modalities": ["video"]},
            ],
        )
    )
    original = httpx.AsyncClient
    monkeypatch.setattr(pollinations.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    models = await pollinations.fetch_models("image")
    assert [m["id"] for m in models] == ["qwen/qwen-image"]
    assert models[0]["aliases"] == ["qwen-image"]
    assert models[0]["publisher"] == "Qwen"


@pytest.mark.asyncio
async def test_transcription_uses_authenticated_current_endpoint(monkeypatch):
    monkeypatch.setattr(provider_keys, "get_provider_key", AsyncMock(return_value="test-key"))
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("transcriptions"):
            return httpx.Response(200, json={"text": "hello"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        pollinations.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    provider = pollinations.PollinationsProvider()
    assert await provider.transcribe_audio(b"audio") == "hello"
    assert [r.url.path for r in requests] == ["/v1/audio/transcriptions"]
    assert all(r.headers["Authorization"] == "Bearer test-key" for r in requests)
    assert all(r.url.host == "gen.pollinations.ai" for r in requests)
