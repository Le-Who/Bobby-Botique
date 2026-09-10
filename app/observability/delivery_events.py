"""Stable field mapping for immutable Telegram delivery outcomes."""

from __future__ import annotations

from app.observability.schema import JsonValue
from app.providers.stream_types import StreamCompleted, StreamFailed
from app.response_delivery.outcomes import CompleteDelivery, DeferredDelivery, FailedDelivery, PartialDelivery


def delivery_outcome_fields(outcome: object) -> dict[str, JsonValue]:
    if not isinstance(outcome, (CompleteDelivery, DeferredDelivery, FailedDelivery, PartialDelivery)):
        raise TypeError(f"Unsupported delivery outcome: {type(outcome).__name__}")
    receipt = outcome.receipt
    fields: dict[str, JsonValue] = {
        "delivery_kind": receipt.kind.value,
        "message_ids": list(receipt.message_ids),
        "final_message_id": receipt.final_message.message_id,
        "publication_present": receipt.publication_url is not None,
        "content_chars": len(getattr(outcome, "content_text", "")),
        "displayed_chars": len(getattr(outcome, "displayed_text", "")),
    }

    if isinstance(outcome, CompleteDelivery):
        fields.update(
            generation_status="completed",
            delivery_status="sent",
            voice_requested=outcome.voice_requested,
        )
        if outcome.completion is not None:
            fields.update(_completion_fields(outcome.completion))
        return fields

    if isinstance(outcome, PartialDelivery):
        fields.update(
            generation_status="partial",
            delivery_status="partial",
            voice_requested=outcome.voice_requested,
        )
        if isinstance(outcome.terminal, StreamFailed):
            fields.update(
                reason_code=outcome.terminal.code.value,
                failure_phase=outcome.terminal.phase.value,
                upstream_error_id=outcome.terminal.error_id,
            )
        else:
            fields.update(_completion_fields(outcome.terminal))
        return fields

    if isinstance(outcome, FailedDelivery):
        fields.update(
            generation_status="failed",
            delivery_status="failure_notice_sent",
            reason_code=outcome.error_code.value,
            upstream_error_id=outcome.upstream_error_id,
        )
        return fields

    if isinstance(outcome, DeferredDelivery):
        fields.update(
            generation_status="deferred",
            delivery_status="deferred",
            task_id=outcome.task_id,
        )
        return fields

    raise AssertionError("unreachable delivery outcome")


def _completion_fields(completion: StreamCompleted) -> dict[str, JsonValue]:
    return {
        "finish_kind": completion.finish_reason.kind.value,
        "token_count": completion.usage.total,
        "provider": completion.route.provider.value,
        "requested_model": completion.route.requested_model,
        "actual_model": completion.route.actual_model,
    }


__all__ = ["delivery_outcome_fields"]
