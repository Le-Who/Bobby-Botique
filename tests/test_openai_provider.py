"""Offline HTTP-boundary coverage for direct Responses and routing isolation."""

import asyncio
import hashlib
import json
from contextlib import aclosing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import agent_use_cases
from app.errors import ErrorCode, extract_error_code
from app.providers import openai
from app.providers.base import get_provider_for_model, is_openai_model, is_openrouter_model
from app.providers.router import ProviderRouter
from app.providers.stream_types import (
    FailurePhase,
    GenerationRequest,
    GroundingMode,
    ImagePart,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    ProviderKind,
    RetryDisposition,
    StreamCompleted,
    StreamFailed,
    TextDelta,
    TextPart,
    ThinkingLevel,
    Workload,
)

MODEL = "openai-direct/gpt-6.1-terra"


def request(**kwargs):
    return GenerationRequest(
        models=(MODEL,),
        turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic input"),)),),
        allow_deferred=False,
        allow_key_race=False,
        **kwargs,
    )


def frame(kind, **kwargs):
    return ("data: " + json.dumps({"type": kind, **kwargs}) + "\n\n").encode()


def completed(**kwargs):
    return frame("response.completed", response={"status": "completed", "model": "gpt-6.1-terra", **kwargs})


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, delay=0, block=False):
        self.chunks = chunks
        self.delay = delay
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture
async def transport(monkeypatch):
    clients = []
    calls = []

    def install(chunks, *, status=200, delay=0, block=False):
        stream = Stream(chunks, delay=delay, block=block)

        async def handler(native_request):
            calls.append(native_request)
            return httpx.Response(status, stream=stream, headers={"content-type": "text/event-stream"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        monkeypatch.setattr(openai, "_openai_http_client", client)
        return stream

    yield install, calls
    for client in clients:
        await client.aclose()


async def collect(req=None):
    return [
        event
        async for event in openai.OpenAIProvider("fake-openai-test-key").stream(req or request(), model_name=MODEL)
    ]


async def test_responses_payload_stream_usage_and_direct_identity(transport):
    install, calls = transport
    stream = install(
        [
            b": keepalive\n\n",
            frame("response.created", response={"status": "in_progress"}),
            frame("response.output_text.delta", delta=" "),
            frame("response.output_text.delta", delta='{"ok":true}'),
            completed(
                usage={
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "total_tokens": 12,
                    "input_tokens_details": {"cached_tokens": 3},
                }
            ),
        ]
    )
    events = await collect(request(system_instruction="synthetic system", thinking_level=ThinkingLevel.OFF))
    assert events[0] == TextDelta(' {"ok":true}')
    assert isinstance(events[1], StreamCompleted)
    assert events[1].usage.total == 12
    assert events[1].usage.cached == 3
    assert events[1].route.provider is ProviderKind.OPENAI
    payload = json.loads(calls[0].content)
    assert str(calls[0].url) == "https://api.openai.com/v1/responses"
    assert payload["model"] == "gpt-6.1-terra"
    assert payload["store"] is False
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["instructions"] == "synthetic system"
    assert payload["input"][0]["content"][0]["type"] == "input_text"
    assert stream.closed
    assert is_openai_model(MODEL) and not is_openrouter_model(MODEL)
    assert isinstance(get_provider_for_model(MODEL, "synthetic"), openai.OpenAIProvider)
    assert is_openrouter_model("some-org/some-model")
    assert is_openrouter_model("openai/gpt-4o-mini")
    assert get_provider_for_model("openai/gpt-4o-mini", "synthetic").provider_name == "openrouter"


async def test_grounding_images_and_model_history_are_preserved(transport):
    install, calls = transport
    stream = install(
        [
            frame("response.output_text.delta", delta="supported answer"),
            completed(
                output=[
                    {"type": "web_search_call", "status": "completed", "action": {"query": "synthetic query"}},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.org/source",
                                        "title": "Synthetic source",
                                    },
                                ],
                            }
                        ],
                    },
                ]
            ),
        ]
    )
    req = GenerationRequest(
        models=(MODEL,),
        turns=(
            PromptTurn(PromptRole.MODEL, (TextPart("earlier answer"),)),
            PromptTurn(PromptRole.USER, (TextPart("question"), ImagePart(b"synthetic-png", "image/png"))),
        ),
        grounding=GroundingMode.PROVIDER_SEARCH_REQUIRED,
    )
    events = await collect(req)
    terminal = events[-1]
    assert isinstance(terminal, StreamCompleted)
    assert terminal.grounding.sources[0].url == "https://example.org/source"
    assert terminal.grounding.search_queries == ("synthetic query",)
    payload = json.loads(calls[0].content)
    assert payload["tool_choice"] == {"type": "web_search"}
    assert payload["input"][0]["role"] == "assistant"
    assert payload["input"][0]["content"] == [{"type": "output_text", "text": "earlier answer", "annotations": []}]
    assert payload["input"][1]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert stream.closed


@pytest.mark.parametrize(
    "chunks,code",
    [
        ([b"data: broken\n\n"], ErrorCode.INVALID_RESPONSE),
        ([frame("response.created", response={})], ErrorCode.INVALID_RESPONSE),
        ([completed()], ErrorCode.EMPTY_RESPONSE),
        ([frame("response.incomplete", response={"status": "incomplete"})], ErrorCode.INVALID_RESPONSE),
        ([frame("response.output_text.delta", delta=42)], ErrorCode.INVALID_RESPONSE),
        ([frame("response.failed", response={"error": {"code": "rate_limit_exceeded"}})], ErrorCode.RATE_LIMIT),
        ([b"data: {}"], ErrorCode.INVALID_RESPONSE),
    ],
)
async def test_protocol_failures_are_terminal_and_close(transport, chunks, code):
    install, _ = transport
    stream = install(chunks)
    events = await collect()
    assert len(events) == 1
    assert isinstance(events[0], StreamFailed)
    assert events[0].code is code
    assert stream.closed


@pytest.mark.parametrize(
    "status,error_code,expected,key",
    [
        (401, None, ErrorCode.INVALID_KEY, KeyDisposition.INVALID),
        (429, "rate_limit_exceeded", ErrorCode.RATE_LIMIT, KeyDisposition.RATE_LIMITED),
        (429, "insufficient_quota", ErrorCode.QUOTA_EXCEEDED, KeyDisposition.EXHAUSTED),
        (400, None, ErrorCode.INVALID_REQUEST, KeyDisposition.UNCHANGED),
        (503, None, ErrorCode.OVERLOADED, KeyDisposition.TRANSIENT_FAILURE),
    ],
)
async def test_http_errors_are_classified_without_body_disclosure(transport, status, error_code, expected, key):
    install, _ = transport
    stream = install(
        [json.dumps({"error": {"code": error_code, "message": "sensitive synthetic body"}}).encode()], status=status
    )
    failure = (await collect())[0]
    assert failure.code is expected
    assert failure.key is key
    assert "sensitive" not in failure.diagnostic
    assert stream.closed


async def test_first_visible_deadline_includes_continuous_metadata(transport, monkeypatch):
    install, _ = transport
    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: 0.025))
    stream = install([frame("response.created", response={})] * 20, delay=0.005)
    events = await collect()
    assert events[0].code is ErrorCode.TIMEOUT
    assert events[0].phase is FailurePhase.BEFORE_TEXT
    assert stream.closed


async def test_overall_deadline_survives_visible_text(transport, monkeypatch):
    install, _ = transport
    monkeypatch.setattr(GenerationRequest, "provider_timeout_seconds", property(lambda self: 0.025))
    stream = install([frame("response.output_text.delta", delta="answer")], block=True)
    events = await collect()
    assert events[0] == TextDelta("answer")
    assert events[1].code is ErrorCode.TIMEOUT
    assert events[1].phase is FailurePhase.AFTER_TEXT
    assert events[1].retry is RetryDisposition.DO_NOT_RETRY
    assert stream.closed


async def test_owner_cancel_and_early_close_release_http_stream(transport):
    install, _ = transport
    stream = install([], block=True)
    task = asyncio.create_task(collect())
    await stream.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed
    stream = install([frame("response.output_text.delta", delta="answer")], block=True)
    async with aclosing(openai.OpenAIProvider("synthetic").stream(request(), model_name=MODEL)) as events:
        assert await anext(events) == TextDelta("answer")
    assert stream.closed


async def test_failure_after_text_never_retries_or_penalizes_key(transport):
    install, _ = transport
    install([frame("response.output_text.delta", delta="partial"), frame("error", error={"code": "invalid_api_key"})])
    events = await collect()
    assert events[-1].phase is FailurePhase.AFTER_TEXT
    assert events[-1].retry is RetryDisposition.DO_NOT_RETRY
    assert events[-1].key is KeyDisposition.UNCHANGED


async def test_unsupported_images_rejected_without_transport(transport):
    _, calls = transport
    req = GenerationRequest(
        models=(MODEL,), turns=(PromptTurn(PromptRole.USER, (ImagePart(b"file", "application/pdf"),)),)
    )
    assert (await collect(req))[0].code is ErrorCode.INVALID_REQUEST
    assert not calls


async def test_required_search_cannot_succeed_without_search(transport):
    install, _ = transport
    install([frame("response.output_text.delta", delta="answer"), completed()])
    events = await collect(request(grounding=GroundingMode.PROVIDER_SEARCH_REQUIRED))
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].phase is FailurePhase.AFTER_TEXT


async def test_direct_key_pool_and_legacy_router_rotate_without_gemini(transport, monkeypatch):
    install, calls = transport
    agent_use_cases._openai_key_health.clear()
    monkeypatch.setattr(agent_use_cases, "_openai_key_cursor", 0)
    monkeypatch.setattr(
        agent_use_cases, "settings", SimpleNamespace(OPENAI_API_KEYS=["synthetic-one", "synthetic-two"])
    )
    monkeypatch.setattr(
        agent_use_cases, "get_available_gemini_key", AsyncMock(side_effect=AssertionError("Gemini fallback"))
    )
    monkeypatch.setattr(
        agent_use_cases, "get_available_openrouter_key", AsyncMock(side_effect=AssertionError("OpenRouter collision"))
    )
    try:
        install([frame("response.failed", response={"error": {"code": "invalid_api_key"}})])
        result, tokens = await ProviderRouter().get_response(MODEL, [{"role": "user", "parts": ["synthetic question"]}])
        assert extract_error_code(result) is ErrorCode.INVALID_KEY
        assert tokens is None
        assert len(calls) == 2
        assert len({call.headers["authorization"] for call in calls}) == 2
        assert len(agent_use_cases._openai_key_health) == 2
        key_hash = hashlib.sha256(b"synthetic-one").hexdigest()[:16]
        assert key_hash in agent_use_cases._openai_key_health
    finally:
        agent_use_cases._openai_key_health.clear()


def test_inline_budget_is_bounded_and_direct_reasoning_default_is_none():
    req = request(workload=Workload.INLINE)
    assert req.first_text_timeout_seconds == 6.0
    assert req.provider_timeout_seconds == 12.0


@pytest.mark.parametrize(
    "model,reasoning",
    [
        ("gpt-6-luna", {"effort": "none"}),
        ("gpt-6.1-terra", {"effort": "none"}),
        ("gpt-6.1-sol", {"effort": "low"}),
        ("gpt-4.1-mini", None),
        ("gpt-4o-mini", None),
    ],
)
async def test_model_reasoning_capabilities_are_explicit(model, reasoning):
    payload = await openai.responses_payload(request(), f"openai-direct/{model}")
    assert payload.get("reasoning") == reasoning


async def test_unsupported_model_and_legacy_search_fail_before_http(transport):
    _, calls = transport
    provider = openai.OpenAIProvider("synthetic")
    events = [event async for event in provider.stream(request(), model_name="openai-direct/unlisted")]
    assert events[0].code is ErrorCode.INVALID_REQUEST
    events = [
        event
        async for event in provider.stream(
            request(grounding=GroundingMode.PROVIDER_SEARCH), model_name="openai-direct/gpt-4o-mini"
        )
    ]
    assert events[0].code is ErrorCode.INVALID_REQUEST
    assert not calls


async def test_malformed_terminal_usage_fails_after_text(transport):
    install, _ = transport
    install([frame("response.output_text.delta", delta="partial"), completed(usage={"total_tokens": -1})])
    events = await collect()
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].phase is FailurePhase.AFTER_TEXT
    assert events[-1].code is ErrorCode.INVALID_RESPONSE


async def test_paid_direct_route_rotates_sequentially_without_key_race(transport, monkeypatch):
    install, calls = transport
    install([frame("response.output_text.delta", delta="answer"), completed()])
    monkeypatch.setattr(agent_use_cases, "settings", SimpleNamespace(OPENAI_API_KEYS=["synthetic-a", "synthetic-b"]))
    req = GenerationRequest(models=(MODEL,), turns=request().turns, allow_deferred=False)
    assert req.allow_key_race  # Existing chat factory default must not double paid calls.
    events = [event async for event in ProviderRouter().stream(req)]
    assert isinstance(events[-1], StreamCompleted)
    assert len(calls) == 1


async def test_three_turn_chat_uses_responses_output_text_for_assistant(transport):
    install, calls = transport
    install([frame("response.output_text.delta", delta="final answer"), completed()])
    req = GenerationRequest(
        models=(MODEL,),
        turns=(
            PromptTurn(PromptRole.USER, (TextPart("first question"),)),
            PromptTurn(PromptRole.MODEL, (TextPart("first answer"),)),
            PromptTurn(PromptRole.USER, (TextPart("follow-up"),)),
        ),
    )
    assert isinstance((await collect(req))[-1], StreamCompleted)
    wire_turns = json.loads(calls[0].content)["input"]
    assert wire_turns == [
        {"role": "user", "content": [{"type": "input_text", "text": "first question"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "first answer", "annotations": []}]},
        {"role": "user", "content": [{"type": "input_text", "text": "follow-up"}]},
    ]


@pytest.mark.parametrize(
    "chunks,status",
    [
        ([frame("response.output_text.delta", delta="answer"), completed()], 200),
        ([frame("response.failed", response={"error": {"code": "invalid_api_key"}})], 200),
        ([completed()], 200),
        ([frame("response.incomplete", response={"status": "incomplete"})], 200),
        ([b'{"error":{"code":"rate_limit_exceeded"}}'], 429),
    ],
)
async def test_http_cleanup_failure_cannot_publish_two_terminal_events(monkeypatch, chunks, status):
    class BrokenClose(Stream):
        async def aclose(self):
            self.closed = True
            raise httpx.ReadError("synthetic cleanup failure")

    stream = BrokenClose(chunks)

    async def handler(native_request):
        return httpx.Response(status, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(openai, "_openai_http_client", client)
        events = await collect()
    terminals = [event for event in events if isinstance(event, (StreamFailed, StreamCompleted))]
    assert len(terminals) == 1
    assert isinstance(terminals[0], StreamFailed)
    assert terminals[0].code is ErrorCode.NETWORK
    assert stream.closed


async def test_sequential_key_rotation_shares_original_first_visible_deadline(monkeypatch):
    monkeypatch.setattr(GenerationRequest, "first_text_timeout_seconds", property(lambda self: 1.0))
    monkeypatch.setattr(agent_use_cases, "settings", SimpleNamespace(OPENAI_API_KEYS=["synthetic-c", "synthetic-d"]))
    streams = []

    async def handler(native_request):
        chunks = (
            [frame("response.failed", response={"error": {"code": "rate_limit_exceeded"}})]
            if not streams
            else [frame("response.output_text.delta", delta="too late"), completed()]
        )
        stream = Stream(chunks, delay=0.4 if not streams else 0.7)
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(openai, "_openai_http_client", client)
            events = [event async for event in ProviderRouter().stream(request())]
        assert len(streams) == 2
        assert not any(isinstance(event, TextDelta) for event in events)
        assert events[-1].code is ErrorCode.TIMEOUT
        assert all(stream.closed for stream in streams)
    finally:
        agent_use_cases._openai_key_health.clear()
