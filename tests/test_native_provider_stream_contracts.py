"""Native SDK/SSE boundaries: typed outcomes, metadata, identity and closure."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.errors import ErrorCode, extract_error_code
from app.providers import gemini, openrouter
from app.providers.base import get_provider_for_model
from app.providers.stream_types import (
    FailurePhase,
    FinishKind,
    GenerationRequest,
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
    TokenUsage,
)
from app.request_context import clear_request_id, set_request_id


class NativeStream:
    """An owned SDK iterator that requires an explicit close, including on cancel."""

    def __init__(self, items, *, block=False):
        self.items = iter(items)
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = next(self.items, None)
        if item is None:
            if self.block:
                self.waiting.set()
                await asyncio.Event().wait()
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        self.closed = True


class HttpStream:
    """HTTP response/context boundary with real status errors and deterministic IO."""

    def __init__(self, lines, *, status=200, body="", block=False):
        self.lines = NativeStream(lines, block=block)
        self.response = httpx.Response(status, text=body, request=httpx.Request("POST", "https://offline.invalid"))
        self.calls = []
        self.closed = False

    def stream(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    def raise_for_status(self):
        self.response.raise_for_status()

    def aiter_lines(self):
        return self.lines


def request_for(model, **kwargs):
    return GenerationRequest(
        models=(model,), turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic question"),)),), **kwargs
    )


def text_frame(kind, text):
    if kind == "opencode":
        return [
            "event: content_block_delta",
            "data: " + json.dumps({"delta": {"type": "text_delta", "text": text}}),
            "",
        ]
    return ["data: " + json.dumps({"choices": [{"delta": {"content": text}}]})]


def sdk_chunk(text, *, finish=None, usage=None):
    return SimpleNamespace(
        text=text, candidates=[SimpleNamespace(finish_reason=finish, grounding_metadata=None)], usage_metadata=usage
    )


def install_native(monkeypatch, kind, items, *, block=False):
    models = {
        "gemini": "gemini-3.5-flash",
        "openrouter": "org/model",
        "opencode": "opencode-go/minimax-m2.7",
    }
    model = models[kind]
    if kind == "gemini":
        stream = NativeStream(items, block=block)

        async def generate_content_stream(**_kwargs):
            return stream

        client = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content_stream=generate_content_stream))
        )
        monkeypatch.setattr(gemini, "get_cached_genai_client", lambda _key: client)
        provider = get_provider_for_model(model, "synthetic-native-key")
    else:
        stream = HttpStream(items, block=block)
        monkeypatch.setattr(openrouter, "_openrouter_http_client", stream)
        provider = get_provider_for_model(model, "synthetic-native-key")
    return provider, stream, request_for(model), model


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini", "openrouter", "opencode"])
@pytest.mark.parametrize("empty", [True, False], ids=["empty", "whitespace"])
async def test_native_empty_or_whitespace_has_one_pre_text_failure_and_closes(monkeypatch, kind, empty):
    # A wrong visible-text check would turn whitespace into success or leak deltas.
    items = [] if empty else ([sdk_chunk(" \n\t")] if kind == "gemini" else text_frame(kind, " \n\t"))
    provider, stream, request, model = install_native(monkeypatch, kind, items)
    events = [event async for event in provider.stream(request, model_name=model)]

    assert len(events) == 1
    terminal = events[0]
    assert isinstance(terminal, StreamFailed)
    assert (terminal.code, terminal.phase, terminal.retry, terminal.key) == (
        ErrorCode.EMPTY_RESPONSE,
        FailurePhase.BEFORE_TEXT,
        RetryDisposition.TRY_NEXT_KEY,
        KeyDisposition.TRANSIENT_FAILURE,
    )
    assert terminal.route.provider is ProviderKind(kind)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini", "openrouter", "opencode"])
async def test_native_finish_without_usage_preserves_text_and_nullable_metadata(monkeypatch, kind):
    # Invented zero usage or lost finish reason breaks the typed terminal contract.
    if kind == "gemini":
        items = [sdk_chunk("Answer"), sdk_chunk(None, finish="STOP", usage=SimpleNamespace(total_token_count=None))]
    elif kind == "opencode":
        items = text_frame(kind, "Answer") + [
            "event: message_delta",
            'data: {"delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":null}}',
            "",
        ]
    else:
        items = text_frame(kind, "Answer") + [
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":null}}',
            "data: [DONE]",
        ]
    provider, stream, request, model = install_native(monkeypatch, kind, items)
    events = [event async for event in provider.stream(request, model_name=model)]

    assert events[0] == TextDelta("Answer")
    assert len(events) == 2
    assert isinstance(events[1], StreamCompleted)
    assert events[1].finish_reason.kind is FinishKind.STOP
    assert events[1].usage == TokenUsage(prompt=None, completion=None, total=None, cached=None)
    assert events[1].route.provider is ProviderKind(kind)
    assert events[1].route.actual_model == model
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini", "openrouter", "opencode"])
@pytest.mark.parametrize("fault", ["transport", "timeout"])
async def test_native_text_then_transport_failure_retains_partial_and_closes(monkeypatch, kind, fault):
    # Retrying/penalizing a key after visible text risks a mixed or duplicated answer.
    error = (
        httpx.ReadError("synthetic transport failure")
        if fault == "transport"
        else httpx.ReadTimeout("synthetic timeout")
    )
    items = ([sdk_chunk("Partial")] if kind == "gemini" else text_frame(kind, "Partial")) + [error]
    provider, stream, request, model = install_native(monkeypatch, kind, items)
    events = [event async for event in provider.stream(request, model_name=model)]

    assert events[0] == TextDelta("Partial")
    assert len(events) == 2
    assert isinstance(events[1], StreamFailed)
    assert (events[1].code, events[1].phase, events[1].retry, events[1].key) == (
        ErrorCode.NETWORK,
        FailurePhase.AFTER_TEXT,
        RetryDisposition.DO_NOT_RETRY,
        KeyDisposition.UNCHANGED,
    )
    assert events[1].route.provider is ProviderKind(kind)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini", "openrouter", "opencode"])
async def test_native_cancellation_propagates_and_awaits_response_close(monkeypatch, kind):
    # Swallowed cancellation or a missing iterator/context close leaks an owned stream.
    provider, stream, request, model = install_native(monkeypatch, kind, [], block=True)

    async def consume():
        return [event async for event in provider.stream(request, model_name=model)]

    task = asyncio.create_task(consume())
    waiting = stream.waiting if kind == "gemini" else stream.lines.waiting
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini", "openrouter", "opencode"])
async def test_native_text_then_upstream_503_keeps_partial_without_retry_or_key_penalty(monkeypatch, kind):
    # Provider-specific HTTP/SDK error classification must survive visible text;
    # transport error bodies must not become deltas or diagnostic content.
    if kind == "gemini":
        error = gemini.APIError(503, {"error": {"message": "synthetic unavailable", "status": "UNAVAILABLE"}})
    else:
        response = httpx.Response(
            503, text="private-upstream-body", request=httpx.Request("POST", "https://offline.invalid")
        )
        error = httpx.HTTPStatusError("synthetic upstream rejection", request=response.request, response=response)
    items = ([sdk_chunk("Partial")] if kind == "gemini" else text_frame(kind, "Partial")) + [error]
    provider, stream, request, model = install_native(monkeypatch, kind, items)
    events = [event async for event in provider.stream(request, model_name=model)]
    assert events[0] == TextDelta("Partial")
    assert len(events) == 2
    assert isinstance(events[1], StreamFailed)
    assert (events[1].code, events[1].phase, events[1].retry, events[1].key) == (
        ErrorCode.OVERLOADED,
        FailurePhase.AFTER_TEXT,
        RetryDisposition.DO_NOT_RETRY,
        KeyDisposition.UNCHANGED,
    )
    assert "private-upstream-body" not in repr(events)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openrouter", "opencode"])
async def test_sse_malformed_frame_is_skipped_before_valid_text(monkeypatch, kind):
    # Invalid JSON must not become user-visible text or mask a later valid frame.
    items = ["event: content_block_delta", "data: {malformed}", ""] + text_frame(kind, "Valid")
    provider, stream, request, model = install_native(monkeypatch, kind, items)
    events = [event async for event in provider.stream(request, model_name=model)]
    assert events[0] == TextDelta("Valid")
    assert len(events) == 2
    assert isinstance(events[1], StreamCompleted)
    assert events[1].usage == TokenUsage()
    assert stream.closed is True


@pytest.mark.asyncio
async def test_messages_sse_multiline_and_final_unterminated_frame_preserve_exact_usage(monkeypatch):
    # Parsing per-line or dropping the final frame loses text/usage in valid Messages SSE.
    items = [
        "event: message_start",
        'data: {"message":{"usage":{"input_tokens":11,"cache_read_input_tokens":3}}}',
        "",
        "event: content_block_delta",
        'data: {"delta":',
        'data: {"type":"text_delta","text":"A"}}',
        "",
        *text_frame("opencode", " B"),
        "event: message_delta",
        'data: {"delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":7}}',
    ]
    provider, stream, request, model = install_native(monkeypatch, "opencode", items)
    events = [event async for event in provider.stream(request, model_name=model)]
    assert events[:2] == [TextDelta("A"), TextDelta(" B")]
    assert len(events) == 3
    assert isinstance(events[2], StreamCompleted)
    assert events[2].finish_reason.kind is FinishKind.MAX_TOKENS
    assert events[2].usage == TokenUsage(prompt=11, completion=7, total=18, cached=3)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,provider_kind",
    [
        ("cat/unlisted-chat-model", ProviderKind.FREETHEAI),
        ("yng/unlisted-chat-model", ProviderKind.FREETHEAI),
        ("vhr/unlisted-chat-model", ProviderKind.FREETHEAI),
        ("unknown-org/unlisted-chat-model", ProviderKind.OPENROUTER),
    ],
)
async def test_factory_unknown_slug_uses_actual_provider_identity(monkeypatch, model, provider_kind):
    # Generic slash routing must not capture known FTA prefixes, even for new models.
    stream = HttpStream(text_frame("openrouter", "Answer"))
    monkeypatch.setattr(openrouter, "_openrouter_http_client", stream)
    provider = get_provider_for_model(model, "synthetic-fta-key")
    events = [event async for event in provider.stream(request_for(model), model_name=model)]
    assert events[0] == TextDelta("Answer")
    assert len(events) == 2
    assert isinstance(events[1], StreamCompleted)
    assert events[1].route.provider is provider_kind
    assert events[1].route.requested_model == model
    assert events[1].route.actual_model == model


@pytest.mark.asyncio
async def test_freetheai_inherited_stream_uses_overrides_and_correlated_header(monkeypatch):
    # Wrong override dispatch changes endpoint/slug/auth or injects unsupported reasoning.
    model = "cat/unlisted-chat-model"
    stream = HttpStream(text_frame("openrouter", "Answer"))
    monkeypatch.setattr(openrouter, "_openrouter_http_client", stream)
    set_request_id("fta-contract-request")
    try:
        provider = get_provider_for_model(model, "synthetic-fta-key")
        events = [
            event
            async for event in provider.stream(request_for(model, thinking_level=ThinkingLevel.HIGH), model_name=model)
        ]
    finally:
        clear_request_id()
    assert events[0] == TextDelta("Answer")
    assert isinstance(events[1], StreamCompleted)
    args, kwargs = stream.calls[0]
    assert args == ("POST", "https://api.freetheai.xyz/v1/chat/completions")
    assert kwargs["json"] == {
        "model": model,
        "messages": [{"role": "user", "content": "synthetic question"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert kwargs["headers"] == {
        "Authorization": "Bearer synthetic-fta-key",
        "Content-Type": "application/json",
        "X-Request-ID": "fta-contract-request",
    }
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code,key,retry",
    [
        (401, "credential rejected", ErrorCode.INVALID_KEY, KeyDisposition.INVALID, RetryDisposition.TRY_NEXT_KEY),
        (
            403,
            "model access requires higher tier",
            ErrorCode.INVALID_REQUEST,
            KeyDisposition.UNCHANGED,
            RetryDisposition.DO_NOT_RETRY,
        ),
        (403, "credential rejected", ErrorCode.INVALID_KEY, KeyDisposition.INVALID, RetryDisposition.TRY_NEXT_KEY),
        (429, "rate limit", ErrorCode.RATE_LIMIT, KeyDisposition.RATE_LIMITED, RetryDisposition.TRY_NEXT_KEY),
    ],
)
async def test_freetheai_http_failure_categories_and_sensitive_body_are_not_exposed(
    monkeypatch, caplog, status, body, code, key, retry
):
    # Model-access rejection must not invalidate a healthy credential or expose a body.
    secret_body = body + " private-prompt-marker Bearer synthetic-fta-key"
    stream = HttpStream([], status=status, body=secret_body)
    monkeypatch.setattr(openrouter, "_openrouter_http_client", stream)
    provider = get_provider_for_model("cat/unlisted-chat-model", "synthetic-fta-key")
    events = [
        event
        async for event in provider.stream(request_for("cat/unlisted-chat-model"), model_name="cat/unlisted-chat-model")
    ]
    assert len(events) == 1
    assert isinstance(events[0], StreamFailed)
    terminal = events[0]
    assert (terminal.code, terminal.phase, terminal.retry, terminal.key) == (code, FailurePhase.BEFORE_TEXT, retry, key)
    assert terminal.route.provider is ProviderKind.FREETHEAI
    assert "private-prompt-marker" not in caplog.text + repr(events)
    assert "synthetic-fta-key" not in caplog.text + repr(events)
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, "credential rejected", ErrorCode.INVALID_KEY),
        (403, "model access requires higher tier", ErrorCode.INVALID_REQUEST),
        (403, "credential rejected", ErrorCode.INVALID_KEY),
        (429, "rate limit", ErrorCode.RATE_LIMIT),
    ],
)
async def test_freetheai_legacy_http_override_preserves_identity_without_sensitive_body(
    monkeypatch, caplog, status, body, code
):
    # The legacy consumer must actually execute FTA's HTTP override while keeping
    # the body/prompt/credential out of logging and the public tagged response.
    model = "cat/unlisted-chat-model"
    sensitive_body = body + " private-prompt-marker Bearer synthetic-fta-key"
    response = httpx.Response(status, text=sensitive_body, request=httpx.Request("POST", "https://offline.invalid"))
    calls = []

    class HttpPost:
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return response

    monkeypatch.setattr(openrouter, "_openrouter_http_client", HttpPost())
    provider = get_provider_for_model(model, "synthetic-fta-key")
    result = await provider.get_response(
        [{"role": "user", "parts": ["synthetic question"]}], model_name=model, max_retries=1
    )
    assert result.success is False
    assert result.provider == "freetheai"
    assert result.model == model
    assert extract_error_code(result.text) is code
    assert len(calls) == 1
    assert calls[0][0] == "https://api.freetheai.xyz/v1/chat/completions"
    assert calls[0][1]["json"]["model"] == model
    assert "private-prompt-marker" not in caplog.text + repr(result)
    assert "synthetic-fta-key" not in caplog.text + repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["stream", "legacy"])
@pytest.mark.parametrize(
    "status,body,code,key,retry",
    [
        pytest.param(
            403,
            "Access denied: invalid API key",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="invalid-api-key",
        ),
        pytest.param(
            403,
            "Invalid access token",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="invalid-access-token",
        ),
        pytest.param(
            403,
            "Model access denied: API key is invalid",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="key-is-invalid",
        ),
        pytest.param(
            403,
            "Model access denied: invalid API-key",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="hyphenated-invalid-key",
        ),
        pytest.param(
            403,
            "Model access denied: access token expired",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="expired-access-token",
        ),
        pytest.param(
            403,
            "Access to model denied: invalid API key",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="model-with-invalid-key",
        ),
        pytest.param(
            403,
            '{"error":{"code":"invalid_api_key","message":"Access denied for model cat/example"}}',
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="structured-invalid-key",
        ),
        pytest.param(
            403,
            "Access denied",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="generic-access-denial",
        ),
        pytest.param(
            403,
            "Access denied for model cat/example",
            ErrorCode.INVALID_REQUEST,
            KeyDisposition.UNCHANGED,
            RetryDisposition.DO_NOT_RETRY,
            id="model-access-denial",
        ),
        pytest.param(
            403,
            "Valid API key does not permit access to this model",
            ErrorCode.INVALID_REQUEST,
            KeyDisposition.UNCHANGED,
            RetryDisposition.DO_NOT_RETRY,
            id="model-with-valid-key",
        ),
        pytest.param(
            403,
            "This tier does not permit access",
            ErrorCode.INVALID_REQUEST,
            KeyDisposition.UNCHANGED,
            RetryDisposition.DO_NOT_RETRY,
            id="tier-access-denial",
        ),
        pytest.param(
            401,
            "Access denied for model cat/example",
            ErrorCode.INVALID_KEY,
            KeyDisposition.INVALID,
            RetryDisposition.TRY_NEXT_KEY,
            id="401-with-model",
        ),
        pytest.param(
            429,
            "Rate limit for model access",
            ErrorCode.RATE_LIMIT,
            KeyDisposition.RATE_LIMITED,
            RetryDisposition.TRY_NEXT_KEY,
            id="rate-limit",
        ),
        pytest.param(
            402,
            "Token quota exhausted for model access",
            ErrorCode.QUOTA_EXCEEDED,
            KeyDisposition.EXHAUSTED,
            RetryDisposition.TRY_NEXT_KEY,
            id="token-quota",
        ),
    ],
)
async def test_freetheai_access_classification_keeps_credential_denial_distinct_from_model_denial(
    monkeypatch, caplog, consumer, status, body, code, key, retry
):
    # The word "access" alone, or a model mentioned beside an explicitly invalid
    # credential, must never terminate key fallback as a model-only request error.
    model = "cat/unlisted-chat-model"
    sensitive_body = body + " private-prompt-marker Bearer synthetic-fta-key"
    response = httpx.Response(status, text=sensitive_body, request=httpx.Request("POST", "https://offline.invalid"))

    class HttpPost:
        async def post(self, *_args, **_kwargs):
            return response

    transport = HttpStream([], status=status, body=sensitive_body) if consumer == "stream" else HttpPost()
    monkeypatch.setattr(openrouter, "_openrouter_http_client", transport)
    provider = get_provider_for_model(model, "synthetic-fta-key")
    if consumer == "stream":
        events = [event async for event in provider.stream(request_for(model), model_name=model)]
        assert len(events) == 1
        assert isinstance(events[0], StreamFailed)
        terminal = events[0]
        assert (terminal.code, terminal.phase, terminal.key, terminal.retry) == (
            code,
            FailurePhase.BEFORE_TEXT,
            key,
            retry,
        )
        assert terminal.route.provider is ProviderKind.FREETHEAI
        assert transport.closed is True
        result_text = repr(events)
    else:
        result = await provider.get_response(
            [{"role": "user", "parts": ["synthetic question"]}], model_name=model, max_retries=1
        )
        assert result.success is False
        assert result.provider == "freetheai"
        assert extract_error_code(result.text) is code
        result_text = repr(result)
    assert "private-prompt-marker" not in caplog.text + result_text
    assert "synthetic-fta-key" not in caplog.text + result_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        pytest.param(403, "Access denied: invalid API key", ErrorCode.INVALID_KEY, id="invalid-key"),
        pytest.param(403, "Invalid access token", ErrorCode.INVALID_KEY, id="invalid-token"),
        pytest.param(403, "Model access requires higher tier", ErrorCode.INVALID_REQUEST, id="model-tier"),
        pytest.param(429, "Rate limit for model access", ErrorCode.RATE_LIMIT, id="rate-limit"),
        pytest.param(402, "Token quota exhausted for model access", ErrorCode.QUOTA_EXCEEDED, id="token-quota"),
    ],
)
async def test_freetheai_access_failure_after_text_does_not_retry_or_penalize_key(
    monkeypatch, caplog, status, body, code
):
    # Once text is visible, credential/model/quota/rate errors retain their code
    # but must not restart generation or change the selected key's disposition.
    model = "cat/unlisted-chat-model"
    sensitive_body = body + " private-prompt-marker Bearer synthetic-fta-key"
    response = httpx.Response(status, text=sensitive_body, request=httpx.Request("POST", "https://offline.invalid"))
    error = httpx.HTTPStatusError("synthetic upstream rejection", request=response.request, response=response)
    transport = HttpStream(text_frame("openrouter", "Partial") + [error])
    monkeypatch.setattr(openrouter, "_openrouter_http_client", transport)
    provider = get_provider_for_model(model, "synthetic-fta-key")
    events = [event async for event in provider.stream(request_for(model), model_name=model)]
    assert events[0] == TextDelta("Partial")
    assert len(events) == 2
    assert isinstance(events[1], StreamFailed)
    assert (events[1].code, events[1].phase, events[1].key, events[1].retry) == (
        code,
        FailurePhase.AFTER_TEXT,
        KeyDisposition.UNCHANGED,
        RetryDisposition.DO_NOT_RETRY,
    )
    assert "private-prompt-marker" not in caplog.text + repr(events)
    assert "synthetic-fta-key" not in caplog.text + repr(events)
    assert transport.closed is True
