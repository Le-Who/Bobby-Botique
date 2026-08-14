"""Typed values shared by provider streaming and its non-UI consumers.

This module intentionally contains no Telegram or provider SDK imports.  It is
the stable boundary between routing/provider code and response delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from app.errors import ErrorCode


class ProviderStreamProtocolError(RuntimeError):
    """A provider adapter violated the typed stream event contract."""


class PromptRole(StrEnum):
    USER = "user"
    MODEL = "model"


class ThinkingLevel(StrEnum):
    AUTO = "auto"
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class GroundingMode(StrEnum):
    NONE = "none"
    PROVIDED_CONTEXT = "provided_context"
    PROVIDER_SEARCH = "provider_search"
    PROVIDER_SEARCH_REQUIRED = "provider_search_required"


class Workload(StrEnum):
    INTERACTIVE = "interactive"
    QUICK_SEARCH = "quick_search"
    DEFERRED_RETRY = "deferred_retry"
    INLINE = "inline"


class ProviderKind(StrEnum):
    GEMINI = "gemini"
    VERTEX = "vertex"
    OPENROUTER = "openrouter"
    OPENCODE = "opencode"
    FREETHEAI = "freetheai"


class FinishKind(StrEnum):
    STOP = "stop"
    MAX_TOKENS = "max_tokens"
    SAFETY = "safety"
    RECITATION = "recitation"
    OTHER = "other"


class FailurePhase(StrEnum):
    BEFORE_TEXT = "before_text"
    AFTER_TEXT = "after_text"


class RetryDisposition(StrEnum):
    TRY_NEXT_KEY = "try_next_key"
    TRY_NEXT_MODEL = "try_next_model"
    RETRY_LATER = "retry_later"
    DO_NOT_RETRY = "do_not_retry"


class KeyDisposition(StrEnum):
    UNCHANGED = "unchanged"
    TRANSIENT_FAILURE = "transient_failure"
    RATE_LIMITED = "rate_limited"
    EXHAUSTED = "exhausted"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("TextPart.text must contain visible text")


@dataclass(frozen=True, slots=True)
class ImagePart:
    data: bytes
    mime_type: str
    needs_compression: bool = False
    cache_key: str | None = None
    task_type: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("ImagePart.data must be non-empty bytes")
        if not isinstance(self.mime_type, str) or not self.mime_type.strip():
            raise ValueError("ImagePart.mime_type must be non-empty")
        if self.cache_key is not None and not self.cache_key.strip():
            raise ValueError("ImagePart.cache_key cannot be blank")
        if self.task_type is not None and not self.task_type.strip():
            raise ValueError("ImagePart.task_type cannot be blank")


PromptPart: TypeAlias = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class PromptTurn:
    role: PromptRole
    parts: tuple[PromptPart, ...]

    def __post_init__(self) -> None:
        normalized = tuple(self.parts)
        if not normalized:
            raise ValueError("PromptTurn.parts must not be empty")
        if not all(isinstance(part, (TextPart, ImagePart)) for part in normalized):
            raise TypeError("PromptTurn.parts must contain typed prompt parts")
        object.__setattr__(self, "parts", normalized)


@dataclass(frozen=True, slots=True)
class RequestScope:
    user_id: int | None = None
    chat_id: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    models: tuple[str, ...]
    turns: tuple[PromptTurn, ...]
    system_instruction: str | None = None
    scope: RequestScope = field(default_factory=RequestScope)
    thinking_level: ThinkingLevel | None = None
    grounding: GroundingMode = GroundingMode.NONE
    workload: Workload = Workload.INTERACTIVE
    allow_deferred: bool = True

    def __post_init__(self) -> None:
        models: list[str] = []
        seen: set[str] = set()
        for model in self.models:
            if not isinstance(model, str) or not model.strip():
                raise ValueError("GenerationRequest.models cannot contain blank names")
            normalized = model.strip()
            if normalized not in seen:
                models.append(normalized)
                seen.add(normalized)
        if not models:
            raise ValueError("GenerationRequest.models must not be empty")

        turns = tuple(self.turns)
        if not turns:
            raise ValueError("GenerationRequest.turns must not be empty")
        if not all(isinstance(turn, PromptTurn) for turn in turns):
            raise TypeError("GenerationRequest.turns must contain PromptTurn values")

        instruction = self.system_instruction
        if instruction is not None and not instruction.strip():
            instruction = None

        object.__setattr__(self, "models", tuple(models))
        object.__setattr__(self, "turns", turns)
        object.__setattr__(self, "system_instruction", instruction)

    @property
    def provider_timeout_seconds(self) -> float:
        """Provider timeout budget selected explicitly by workload."""
        return 45.0 if self.workload is Workload.INLINE else 120.0

    @property
    def key_attempt_rounds(self) -> int:
        """Preserve the established inline key-rotation budget."""
        return 4 if self.workload is Workload.INLINE else 3


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("TextDelta.text must contain visible text")


class VisibleTextBuffer:
    """Preserve semantic whitespace without allowing it to win a provider race."""

    __slots__ = ("_pending",)

    def __init__(self) -> None:
        self._pending = ""

    def push(self, text: str) -> TextDelta | None:
        if not isinstance(text, str):
            raise TypeError("Provider text chunks must be strings")
        if not text:
            return None
        self._pending += text
        if not self._pending.strip():
            return None
        delta = TextDelta(self._pending)
        self._pending = ""
        return delta


@dataclass(frozen=True, slots=True)
class FinishReason:
    kind: FinishKind
    raw: str | None = None

    @classmethod
    def from_raw(cls, raw: str | None) -> FinishReason:
        normalized = raw.upper().strip() if isinstance(raw, str) else None
        if normalized in {"STOP", "1", "END_TURN", "FINISH_REASON_STOP"}:
            kind = FinishKind.STOP
        elif normalized in {
            "MAX_TOKENS",
            "3",
            "LENGTH",
            "FINISH_REASON_MAX_TOKENS",
        }:
            kind = FinishKind.MAX_TOKENS
        elif normalized in {
            "SAFETY",
            "BLOCKLIST",
            "PROHIBITED_CONTENT",
            "SPII",
            "MALFORMED_FUNCTION_CALL",
        }:
            kind = FinishKind.SAFETY
        elif normalized == "RECITATION":
            kind = FinishKind.RECITATION
        else:
            kind = FinishKind.OTHER
        return cls(kind=kind, raw=raw)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt: int | None = None
    completion: int | None = None
    total: int | None = None
    cached: int | None = None

    def __post_init__(self) -> None:
        for name in ("prompt", "completion", "total", "cached"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError(f"TokenUsage.{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class GroundingSource:
    url: str
    title: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("GroundingSource.url must be non-empty")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("GroundingSource.title must be non-empty")


@dataclass(frozen=True, slots=True)
class GroundingReport:
    sources: tuple[GroundingSource, ...] = ()
    search_queries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        sources = tuple(self.sources)
        queries = tuple(query for query in self.search_queries if query.strip())
        if not all(isinstance(source, GroundingSource) for source in sources):
            raise TypeError("GroundingReport.sources must contain GroundingSource values")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "search_queries", queries)


@dataclass(frozen=True, slots=True)
class RouteUsed:
    provider: ProviderKind
    requested_model: str
    actual_model: str

    def __post_init__(self) -> None:
        if not self.requested_model.strip() or not self.actual_model.strip():
            raise ValueError("RouteUsed model names must be non-empty")


@dataclass(frozen=True, slots=True)
class StreamCompleted:
    finish_reason: FinishReason
    usage: TokenUsage
    grounding: GroundingReport
    route: RouteUsed


@dataclass(frozen=True, slots=True)
class StreamFailed:
    code: ErrorCode
    phase: FailurePhase
    retry: RetryDisposition
    key: KeyDisposition
    diagnostic: str
    route: RouteUsed | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.diagnostic, str) or not self.diagnostic.strip():
            raise ValueError("StreamFailed.diagnostic must be non-empty")


@dataclass(frozen=True, slots=True)
class StreamDeferred:
    task_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("StreamDeferred.task_id must be non-empty")


GenerationTerminalEvent: TypeAlias = StreamCompleted | StreamFailed | StreamDeferred
GenerationEvent: TypeAlias = TextDelta | GenerationTerminalEvent


def is_terminal_event(event: GenerationEvent) -> bool:
    return isinstance(event, (StreamCompleted, StreamFailed, StreamDeferred))


__all__ = [
    "FailurePhase",
    "FinishKind",
    "FinishReason",
    "GenerationEvent",
    "GenerationRequest",
    "GenerationTerminalEvent",
    "GroundingMode",
    "GroundingReport",
    "GroundingSource",
    "ImagePart",
    "KeyDisposition",
    "PromptPart",
    "PromptRole",
    "PromptTurn",
    "ProviderKind",
    "ProviderStreamProtocolError",
    "RequestScope",
    "RetryDisposition",
    "RouteUsed",
    "StreamCompleted",
    "StreamDeferred",
    "StreamFailed",
    "TextDelta",
    "TextPart",
    "ThinkingLevel",
    "TokenUsage",
    "VisibleTextBuffer",
    "Workload",
    "is_terminal_event",
]
