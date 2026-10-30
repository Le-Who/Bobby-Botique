"""Business outcomes shared by queue workers and their registered handlers."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class JobOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN_DELIVERY = "unknown_delivery"


class QueueDisposition(StrEnum):
    """Evidence from an attempted ACK/NACK, separate from handler success."""

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    CAPACITY_REJECTED = "capacity_rejected"


@dataclass(frozen=True, slots=True)
class JobResult:
    outcome: JobOutcome
    reason_code: str | None = None
    result_summary: Mapping[str, int | str | bool] = field(default_factory=dict)
    retry_after: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_summary", MappingProxyType(dict(self.result_summary)))


class AdmissionState(StrEnum):
    ACCEPTED = "accepted"
    EXISTING = "existing"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class QueueAdmission:
    state: AdmissionState
    task_id: str | None
    reason_code: str | None = None
    storage_mode: str = "local"


class JobState(StrEnum):
    QUEUED = "queued"
    DELAYED = "delayed"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN_DELIVERY = "unknown_delivery"


TERMINAL_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED, JobState.UNKNOWN_DELIVERY}
)


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    max_jobs: int = 100
    max_payload_bytes: int = 256 * 1024
    max_total_payload_bytes: int = 8 * 1024 * 1024
    max_age_seconds: int = 900
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in self.as_dict().values()):
            raise ValueError("Queue policy limits must be positive integers")

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


DEFAULT_QUEUE_POLICY = QueuePolicy()
DAILY_QUEUE_POLICY = QueuePolicy(max_jobs=32, max_age_seconds=1800)
BROADCAST_QUEUE_POLICY = QueuePolicy(max_jobs=10)

PUBLIC_SUMMARY_FIELDS = frozenset(
    {
        "phase",
        "total",
        "completed",
        "processed",
        "remaining",
        "failed",
        "skipped",
        "generated",
        "prepared",
        "updated",
        "partial",
        "ready",
        "date",
        "game",
        "language",
        "difficulty",
        "mode",
        "actual_model",
        "model",
        "settings_revision",
        "pages",
        "text_length",
        "paragraphs",
        "tables",
        "images_generated",
        "images_failed",
        "items_ready",
        "items_failed",
        "items_total",
        "error_count",
        "artifacts_prepared",
        "artifacts_failed",
        "tasks_completed",
        "tasks_total",
        "ok",
        "status",
        "effective_revision",
        "effective_models",
        "part",
        "model_plan",
        "puzzle_revision",
    }
)


def public_summary(value: Mapping[str, int | str | bool]) -> dict[str, int | str | bool]:
    clean = {}
    for key, item in value.items():
        if key not in PUBLIC_SUMMARY_FIELDS or type(item) not in {int, str, bool}:
            raise QueueConflict("invalid_public_summary")
        if isinstance(item, str):
            pattern = (
                r"[A-Za-z0-9_./:+,; -]{0,512}"
                if key in {"effective_models", "model_plan"}
                else r"[A-Za-z0-9_./:+-]{1,128}"
            )
            if re.fullmatch(pattern, item) is None:
                raise QueueConflict("invalid_public_summary")
        if type(item) is int and not 0 <= item <= 2**53 - 1:
            raise QueueConflict("invalid_public_summary")
        clean[key] = item
    return clean


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    task_id: str
    kind: str
    lane: str
    state: JobState
    revision: int
    attempts_started: int
    max_attempts: int
    submitted_at: float
    started_at: float | None
    finished_at: float | None
    not_before: float
    deadline_at: float
    heartbeat_at: float | None
    reason_code: str | None
    storage_mode: str
    retry_eligible: bool
    retry_payload_available: bool
    parent_task_id: str | None = None
    result_summary: Mapping[str, int | str | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_summary", MappingProxyType(dict(self.result_summary)))

    def as_dict(self) -> dict[str, Any]:
        return {
            name: dict(value) if name == "result_summary" else value.value if isinstance(value, StrEnum) else value
            for name in self.__dataclass_fields__
            for value in (getattr(self, name),)
        }


@dataclass(frozen=True, slots=True)
class JobPage:
    items: tuple[JobSnapshot, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class LaneSnapshot:
    lane: str
    revision: int
    admission_paused: bool
    claim_paused: bool
    policy: QueuePolicy
    workers: int


class QueueError(RuntimeError):
    """A bounded public queue command error; reason_code never contains payload."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class QueueUnavailable(QueueError):
    pass


class QueueConflict(QueueError):
    pass


class QueueAdmissionUnconfirmed(QueueUnavailable):
    def __init__(self, task_id: str | None):
        self.task_id = task_id
        super().__init__("admission_unconfirmed")


class JobOwnershipLost(QueueError):
    pass


class JobCancelRequested(QueueError):
    pass


def normalize_job_result(value: object) -> JobResult:
    """Recognize explicit legacy states during the versioned queue transition.

    Legacy failures lack retry eligibility, so they are terminal. Never copy
    their free-form errors or arbitrary return payload into public job state.
    Registered production handlers return JobResult directly.
    """
    if isinstance(value, JobResult):
        return value
    if isinstance(value, dict) and value.get("status") == "completed":
        summary = {
            key: item
            for key, item in value.items()
            if key in {"pages", "text_length", "paragraphs", "tables"} and type(item) is int
        }
        return JobResult(JobOutcome.SUCCEEDED, result_summary=summary)
    if isinstance(value, dict) and value.get("status") == "failed":
        return JobResult(JobOutcome.PERMANENT_FAILURE, "legacy_handler_failed")
    return JobResult(JobOutcome.PERMANENT_FAILURE, "invalid_handler_result")
