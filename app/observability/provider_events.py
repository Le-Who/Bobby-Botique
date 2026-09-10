"""Provider-attempt instrumentation shared by all typed streaming adapters."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.errors import ErrorCode
from app.observability.events import emit, record_exception
from app.observability.redaction import provider_key_fields
from app.observability.schema import JsonValue
from app.providers.stream_types import (
    GenerationEvent,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    is_terminal_event,
)


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    """Safe provider-attempt identity; raw credentials are never retained."""

    attempt_id: str
    started_ns: int
    provider: str
    requested_model: str
    actual_model: str
    key_present: bool
    key_suffix: str | None
    key_fingerprint: str | None
    attempt_number: int | None = None
    max_attempts: int | None = None
    race_id: str | None = None
    key_source: str | None = None

    def fields(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "key_present": self.key_present,
            "key_suffix": self.key_suffix,
            "key_fingerprint": self.key_fingerprint,
            "attempt_number": self.attempt_number,
            "max_attempts": self.max_attempts,
            "race_id": self.race_id,
            "key_source": self.key_source,
        }


def start_provider_attempt(
    *,
    provider: str,
    requested_model: str,
    actual_model: str,
    api_key: str | None,
    key_hash: str | None = None,
    attempt_number: int | None = None,
    max_attempts: int | None = None,
    race_id: str | None = None,
    key_source: str | None = None,
) -> ProviderAttempt:
    """Create and log one real provider call attempt."""
    key_identity = provider_key_fields(provider, api_key, key_hash=key_hash)
    attempt = ProviderAttempt(
        attempt_id=uuid.uuid4().hex,
        started_ns=time.monotonic_ns(),
        provider=provider,
        requested_model=requested_model,
        actual_model=actual_model,
        key_present=bool(key_identity["key_present"]),
        key_suffix=key_identity["key_suffix"] if isinstance(key_identity["key_suffix"], str) else None,
        key_fingerprint=(key_identity["key_fingerprint"] if isinstance(key_identity["key_fingerprint"], str) else None),
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        race_id=race_id,
        key_source=key_source,
    )
    emit(
        "provider.attempt_started",
        operation="provider.request",
        **attempt.fields(),
    )
    return attempt


def record_provider_exception(
    error: BaseException,
    *,
    provider: str,
    model: str,
    api_key: str | None,
    key_hash: str | None = None,
    failure_phase: str,
    **fields: JsonValue,
) -> str:
    """Capture a provider-adapter exception before it becomes a typed terminal."""
    return record_exception(
        "provider.call_failed",
        error,
        operation="provider.request",
        fields={
            "provider": provider,
            "actual_model": model,
            "failure_phase": failure_phase,
            **provider_key_fields(provider, api_key, key_hash=key_hash),
            **fields,
        },
    )


def record_provider_validation_failure(
    *,
    provider: str,
    requested_model: str,
    actual_model: str,
    api_key: str | None,
    validation_code: str,
) -> None:
    """Record a terminal local-validation result before any HTTP attempt."""
    emit(
        "provider.local_validation_finished",
        level="warning",
        operation="provider.local_validation",
        outcome="failed",
        reason_code="invalid_request",
        failure_phase="local_validation",
        validation_code=validation_code,
        provider=provider,
        requested_model=requested_model,
        actual_model=actual_model,
        transport_started=False,
        **provider_key_fields(provider, api_key),
    )


def _duration_ms(attempt: ProviderAttempt) -> float:
    return round(max(0, time.monotonic_ns() - attempt.started_ns) / 1_000_000, 2)


def _terminal_fields(terminal: GenerationEvent) -> tuple[str, str, dict[str, JsonValue]]:
    if isinstance(terminal, StreamCompleted):
        return (
            "succeeded",
            "info",
            {
                "finish_kind": terminal.finish_reason.kind.value,
                "finish_reason_raw": terminal.finish_reason.raw,
                "prompt_tokens": terminal.usage.prompt,
                "completion_tokens": terminal.usage.completion,
                "token_count": terminal.usage.total,
                "cached_tokens": terminal.usage.cached,
                "grounding_sources": len(terminal.grounding.sources),
                "grounding_queries": len(terminal.grounding.search_queries),
            },
        )
    if isinstance(terminal, StreamFailed):
        return (
            "failed",
            "warning",
            {
                "reason_code": terminal.code.value,
                "failure_phase": terminal.phase.value,
                "retry_disposition": terminal.retry.value,
                "key_disposition": terminal.key.value,
                "diagnostic": terminal.diagnostic,
                "error_id": terminal.error_id,
            },
        )
    if isinstance(terminal, StreamDeferred):
        return "deferred", "info", {"task_id": terminal.task_id}
    raise TypeError(f"Unsupported provider terminal: {type(terminal).__name__}")


async def observe_provider_stream(
    events: AsyncIterator[GenerationEvent],
    *,
    provider: str,
    requested_model: str,
    actual_model: str,
    api_key: str | None,
    key_hash: str | None = None,
    attempt_number: int | None = None,
    max_attempts: int | None = None,
    race_id: str | None = None,
    key_source: str | None = None,
) -> AsyncIterator[GenerationEvent]:
    """Yield unchanged provider events while recording one attempt lifecycle."""
    attempt = start_provider_attempt(
        provider=provider,
        requested_model=requested_model,
        actual_model=actual_model,
        api_key=api_key,
        key_hash=key_hash,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        race_id=race_id,
        key_source=key_source,
    )
    saw_text = False
    terminal_seen = False
    try:
        async for event in events:
            if isinstance(event, TextDelta) and not saw_text:
                saw_text = True
                emit(
                    "provider.first_text",
                    operation="provider.request",
                    ttft_ms=_duration_ms(attempt),
                    first_delta_chars=len(event.text),
                    **attempt.fields(),
                )
            if is_terminal_event(event):
                terminal_seen = True
                if isinstance(event, StreamCompleted) and not saw_text:
                    outcome = "failed"
                    level = "warning"
                    terminal_fields: dict[str, JsonValue] = {
                        "reason_code": ErrorCode.EMPTY_RESPONSE.value,
                        "failure_phase": "before_text",
                        "retry_disposition": "try_next_key",
                        "key_disposition": "transient_failure",
                        "finish_kind": event.finish_reason.kind.value,
                        "finish_reason_raw": event.finish_reason.raw,
                    }
                else:
                    outcome, level, terminal_fields = _terminal_fields(event)
                emit(
                    "provider.attempt_finished",
                    level=level,
                    operation="provider.request",
                    outcome=outcome,
                    duration_ms=_duration_ms(attempt),
                    text_emitted=saw_text,
                    **attempt.fields(),
                    **terminal_fields,
                )
            yield event
        if not terminal_seen:
            emit(
                "provider.attempt_finished",
                level="error",
                operation="provider.request",
                outcome="failed",
                reason_code="protocol_missing_terminal",
                duration_ms=_duration_ms(attempt),
                text_emitted=saw_text,
                **attempt.fields(),
            )
    except asyncio.CancelledError:
        emit(
            "provider.attempt_finished",
            level="warning",
            operation="provider.request",
            outcome="cancelled",
            cancellation_reason="race_loser_or_parent_cancelled",
            duration_ms=_duration_ms(attempt),
            text_emitted=saw_text,
            **attempt.fields(),
        )
        raise
    except Exception as error:
        error_id = record_exception(
            "provider.attempt_failed",
            error,
            operation="provider.request",
            fields=attempt.fields(),
        )
        emit(
            "provider.attempt_finished",
            level="error",
            operation="provider.request",
            outcome="failed",
            reason_code="exception",
            error_id=error_id,
            duration_ms=_duration_ms(attempt),
            text_emitted=saw_text,
            **attempt.fields(),
        )
        raise
