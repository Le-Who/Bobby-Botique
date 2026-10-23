"""Regression coverage for stream ownership and provider-reported failures."""

import json

import httpx
import pytest

from app.errors import ErrorCode
from app.providers import openrouter
from app.providers.base import get_provider_for_model
from app.providers.stream_types import (
    FailurePhase,
    GenerationRequest,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    RetryDisposition,
    StreamFailed,
    TextDelta,
    TextPart,
)


def _request(model: str) -> GenerationRequest:
    return GenerationRequest(models=(model,), turns=(PromptTurn(PromptRole.USER, (TextPart("question"),)),))


class _HttpStream:
    def __init__(self, frames):
        self.frames = frames
        self.closed = False
        self.response = httpx.Response(200, request=httpx.Request("POST", "https://offline.invalid"))

    def stream(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        self.response.raise_for_status()

    async def aiter_lines(self):
        for frame in self.frames:
            yield "data: " + json.dumps(frame)


async def test_closing_opencode_chat_iterator_closes_its_http_response_immediately(monkeypatch):
    transport = _HttpStream([{"choices": [{"delta": {"content": "partial answer"}}]}])
    monkeypatch.setattr(openrouter, "_openrouter_http_client", transport)
    model = "opencode-go/glm-5"
    provider = get_provider_for_model(model, "synthetic-key")
    stream = provider.stream(_request(model), model_name=model)

    try:
        assert await anext(stream) == TextDelta("partial answer")
    finally:
        await stream.aclose()

    assert transport.closed is True


@pytest.mark.parametrize("model", ["org/model", "opencode-go/glm-5", "cat/unlisted-chat-model"])
@pytest.mark.parametrize("partial", [False, True], ids=["before-text", "after-text"])
@pytest.mark.parametrize(
    ("error_code", "expected_code", "expected_key"),
    [
        (429, ErrorCode.RATE_LIMIT, KeyDisposition.RATE_LIMITED),
        (503, ErrorCode.OVERLOADED, KeyDisposition.TRANSIENT_FAILURE),
        ("server_error", ErrorCode.GENERIC, KeyDisposition.TRANSIENT_FAILURE),
    ],
)
async def test_openai_compatible_error_frame_is_terminal_failure(
    monkeypatch, model, partial, error_code, expected_code, expected_key
):
    frames = [{"choices": [{"delta": {"content": "partial answer"}}]}] if partial else []
    frames.append(
        {
            "error": {"code": error_code, "message": "synthetic upstream failure"},
            "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "error"}],
        }
    )
    transport = _HttpStream(frames)
    monkeypatch.setattr(openrouter, "_openrouter_http_client", transport)
    provider = get_provider_for_model(model, "synthetic-key")

    events = [event async for event in provider.stream(_request(model), model_name=model)]

    assert len(events) == (2 if partial else 1)
    failure = events[-1]
    assert isinstance(failure, StreamFailed)
    assert failure.code is expected_code
    assert failure.phase is (FailurePhase.AFTER_TEXT if partial else FailurePhase.BEFORE_TEXT)
    assert failure.retry is (RetryDisposition.DO_NOT_RETRY if partial else RetryDisposition.TRY_NEXT_KEY)
    assert failure.key is (KeyDisposition.UNCHANGED if partial else expected_key)
    assert transport.closed is True
