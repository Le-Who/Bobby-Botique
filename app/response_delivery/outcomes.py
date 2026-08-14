"""Immutable outcomes returned to Telegram handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from app.errors import ErrorCode
from app.providers.stream_types import StreamCompleted, StreamFailed
from app.response_delivery.renderer import DeliveryReceipt


@dataclass(frozen=True, slots=True)
class CompleteDelivery:
    content_text: str
    displayed_text: str
    completion: StreamCompleted | None
    voice_requested: bool
    receipt: DeliveryReceipt


@dataclass(frozen=True, slots=True)
class PartialDelivery:
    content_text: str
    displayed_text: str
    terminal: StreamCompleted | StreamFailed
    voice_requested: bool
    receipt: DeliveryReceipt


@dataclass(frozen=True, slots=True)
class FailedDelivery:
    error_code: ErrorCode
    displayed_text: str
    receipt: DeliveryReceipt
    content_text: str = ""


@dataclass(frozen=True, slots=True)
class DeferredDelivery:
    task_id: str
    displayed_text: str
    receipt: DeliveryReceipt
    content_text: str = ""


TelegramResponseOutcome: TypeAlias = (
    CompleteDelivery | PartialDelivery | FailedDelivery | DeferredDelivery
)


__all__ = [
    "CompleteDelivery",
    "DeferredDelivery",
    "FailedDelivery",
    "PartialDelivery",
    "TelegramResponseOutcome",
]
