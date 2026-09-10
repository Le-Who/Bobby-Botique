"""Lifecycle events for direct SDK/HTTP workloads outside the chat router."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass

from app.observability.events import emit, record_exception
from app.observability.redaction import provider_key_fields
from app.observability.schema import JsonValue


@dataclass(frozen=True, slots=True)
class WorkloadAttempt:
    attempt_id: str
    started_ns: int
    workload: str
    provider: str
    model: str | None
    origin: str | None
    key_present: bool
    key_suffix: str | None
    key_fingerprint: str | None

    def fields(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "workload": self.workload,
            "provider": self.provider,
            "model": self.model,
            "origin": self.origin,
            "key_present": self.key_present,
            "key_suffix": self.key_suffix,
            "key_fingerprint": self.key_fingerprint,
        }

    def finish(self, **fields: JsonValue) -> None:
        outcome_value = fields.pop("outcome", "unknown")
        outcome = outcome_value if isinstance(outcome_value, str) else "unknown"
        level_value = fields.pop("level", "info")
        level = level_value if isinstance(level_value, str) else "info"
        emit(
            "workload.attempt_finished",
            level=level,
            operation=f"{self.workload}.request",
            outcome=outcome,
            duration_ms=round((time.monotonic_ns() - self.started_ns) / 1_000_000, 2),
            **self.fields(),
            **fields,
        )

    def fail(self, error: BaseException, *, reason_code: str = "exception", **fields: JsonValue) -> str:
        evidence = {**self.fields(), "reason_code": reason_code, **fields}
        error_id = record_exception(
            "workload.attempt_failed",
            error,
            operation=f"{self.workload}.request",
            fields=evidence,
        )
        self.finish(
            outcome="failed",
            level="error",
            error_id=error_id,
            reason_code=reason_code,
            **fields,
        )
        return error_id


def start_workload_attempt(
    *,
    workload: str,
    provider: str,
    model: str | None,
    api_key: str | None,
    key_hash: str | None = None,
    origin: str | None = None,
    **fields: JsonValue,
) -> WorkloadAttempt:
    identity = provider_key_fields(provider, api_key, key_hash=key_hash)
    attempt = WorkloadAttempt(
        attempt_id=uuid.uuid4().hex,
        started_ns=time.monotonic_ns(),
        workload=workload,
        provider=provider,
        model=model,
        origin=origin,
        key_present=bool(identity["key_present"]),
        key_suffix=identity["key_suffix"] if isinstance(identity["key_suffix"], str) else None,
        key_fingerprint=(identity["key_fingerprint"] if isinstance(identity["key_fingerprint"], str) else None),
    )
    emit(
        "workload.attempt_started",
        operation=f"{workload}.request",
        **attempt.fields(),
        **fields,
    )
    return attempt


async def observe_workload_call[T](
    awaitable: Awaitable[T],
    *,
    workload: str,
    provider: str,
    model: str | None,
    api_key: str | None,
    key_hash: str | None = None,
    origin: str | None = None,
    **fields: JsonValue,
) -> T:
    """Await one direct provider call with guaranteed key-aware terminal evidence."""
    import asyncio

    attempt = start_workload_attempt(
        workload=workload,
        provider=provider,
        model=model,
        api_key=api_key,
        key_hash=key_hash,
        origin=origin,
        **fields,
    )
    try:
        result = await awaitable
    except asyncio.CancelledError:
        attempt.finish(outcome="cancelled", level="warning", reason_code="cancelled")
        raise
    except Exception as error:
        attempt.fail(error, reason_code="exception")
        raise
    attempt.finish(outcome="succeeded")
    return result


__all__ = ["WorkloadAttempt", "observe_workload_call", "start_workload_attempt"]
