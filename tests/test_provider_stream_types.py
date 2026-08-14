"""Contract tests for the typed provider-stream boundary."""

from dataclasses import FrozenInstanceError

import pytest

from app.errors import ErrorCode
from app.providers.stream_types import (
    FailurePhase,
    FinishKind,
    FinishReason,
    GenerationRequest,
    GroundingMode,
    GroundingReport,
    GroundingSource,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    ProviderKind,
    RequestScope,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    TextPart,
    ThinkingLevel,
    TokenUsage,
    Workload,
)
from app.utils.image_utils import TaggedImage


def test_generation_request_is_immutable_and_normalizes_models():
    request = GenerationRequest(
        models=(" gemini-3.5-flash ", "gemini-3.5-flash", "fallback"),
        turns=(PromptTurn(PromptRole.USER, (TextPart("Hello"),)),),
        scope=RequestScope(user_id=1, chat_id=-100),
        thinking_level=ThinkingLevel.HIGH,
        grounding=GroundingMode.PROVIDER_SEARCH,
        workload=Workload.INTERACTIVE,
        allow_deferred=True,
    )

    assert request.models == ("gemini-3.5-flash", "fallback")
    with pytest.raises(FrozenInstanceError):
        request.allow_deferred = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"models": ()},
        {"models": ("",)},
        {"turns": ()},
    ],
)
def test_generation_request_rejects_incomplete_protocol_values(kwargs):
    valid = {
        "models": ("gemini-3.5-flash",),
        "turns": (PromptTurn(PromptRole.USER, (TextPart("Hello"),)),),
    }
    valid.update(kwargs)

    with pytest.raises(ValueError):
        GenerationRequest(**valid)


def test_text_delta_requires_visible_text():
    with pytest.raises(ValueError):
        TextDelta("")
    with pytest.raises(ValueError):
        TextDelta("   \n")

    assert TextDelta(" answer ").text == " answer "


def test_visible_text_buffer_preserves_whitespace_between_provider_chunks():
    from app.providers.stream_types import VisibleTextBuffer

    buffer = VisibleTextBuffer()

    assert buffer.push("Hello") == TextDelta("Hello")
    assert buffer.push(" ") is None
    assert buffer.push("world") == TextDelta(" world")


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("STOP", FinishKind.STOP),
        ("1", FinishKind.STOP),
        ("MAX_TOKENS", FinishKind.MAX_TOKENS),
        ("SAFETY", FinishKind.SAFETY),
        ("RECITATION", FinishKind.RECITATION),
        ("provider_new_reason", FinishKind.OTHER),
        (None, FinishKind.OTHER),
    ],
)
def test_finish_reason_normalizes_known_values_and_preserves_raw(raw, kind):
    reason = FinishReason.from_raw(raw)

    assert reason.kind is kind
    assert reason.raw == raw


def test_completed_event_carries_exact_nullable_metadata():
    event = StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(prompt=10, completion=20, total=30, cached=None),
        grounding=GroundingReport(
            sources=(GroundingSource(url="https://example.com", title="Example"),)
        ),
        route=RouteUsed(
            provider=ProviderKind.GEMINI,
            requested_model="gemini-3.5-flash",
            actual_model="gemini-3.5-flash",
        ),
    )

    assert event.usage.total == 30
    assert event.usage.cached is None
    assert event.grounding.sources[0].url == "https://example.com"


def test_usage_rejects_negative_provider_counts():
    with pytest.raises(ValueError):
        TokenUsage(total=-1)


def test_failure_and_deferred_events_are_typed_terminal_values():
    failure = StreamFailed(
        code=ErrorCode.RATE_LIMIT,
        phase=FailurePhase.BEFORE_TEXT,
        retry=RetryDisposition.TRY_NEXT_KEY,
        key=KeyDisposition.RATE_LIMITED,
        diagnostic="HTTP 429 from provider",
    )
    deferred = StreamDeferred(task_id="task-123")

    assert failure.code is ErrorCode.RATE_LIMIT
    assert failure.route is None
    assert deferred.task_id == "task-123"
    with pytest.raises(ValueError):
        StreamDeferred(task_id=" ")


@pytest.mark.asyncio
async def test_request_factory_converts_legacy_history_at_one_boundary():
    from app.providers.request_factory import generation_request_from_history

    request = await generation_request_from_history(
        models=("gemini-3.5-flash", "fallback"),
        history=[
            {"role": "user", "parts": ["look", TaggedImage(b"\x89PNG\r\n\x1a\nbytes", pre_compressed=True)]},
            {"role": "assistant", "parts": ["seen"]},
        ],
        user_id=7,
        chat_id=-100,
        thinking_level="low",
        grounding=GroundingMode.PROVIDER_SEARCH,
        workload=Workload.INLINE,
        allow_deferred=False,
    )

    assert request.models == ("gemini-3.5-flash", "fallback")
    assert request.turns[0].role is PromptRole.USER
    assert request.turns[0].parts[1].mime_type == "image/png"  # type: ignore[union-attr]
    assert request.turns[0].parts[1].needs_compression is False  # type: ignore[union-attr]
    assert request.turns[1].role is PromptRole.MODEL
    assert request.thinking_level is ThinkingLevel.LOW
    assert request.scope == RequestScope(user_id=7, chat_id=-100)
