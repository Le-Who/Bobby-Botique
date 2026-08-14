"""Pure preparation of canonical response text and Telegram actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from telegram import InlineKeyboardMarkup

from app.providers.stream_types import GenerationTerminalEvent
from app.response_delivery.normalization import strip_hallucinated_tool_trace
from app.utils.response_tags import parse_response_tags


@dataclass(frozen=True, slots=True)
class PresentationFacts:
    raw_content: str
    terminal: GenerationTerminalEvent | None
    voice_requested: bool


@dataclass(frozen=True, slots=True)
class PreparedPresentation:
    content_text: str
    actions: InlineKeyboardMarkup | None
    recovery_actions: InlineKeyboardMarkup | None
    failure_actions: InlineKeyboardMarkup | None
    display_prefix: str
    footer: str
    long_read_title: str


class TelegramPresentation(Protocol):
    def prepare(self, facts: PresentationFacts) -> PreparedPresentation: ...


@dataclass(frozen=True, slots=True)
class FixedPresentation:
    actions: InlineKeyboardMarkup | None = None
    recovery_actions: InlineKeyboardMarkup | None = None
    failure_actions: InlineKeyboardMarkup | None = None
    display_prefix: str = ""
    footer: str = ""
    long_read_title: str = "Ответ ИИ"

    def prepare(self, facts: PresentationFacts) -> PreparedPresentation:
        content = strip_hallucinated_tool_trace(facts.raw_content)
        if content.lstrip().startswith("[VOICE]"):
            leading = len(content) - len(content.lstrip())
            content = content[:leading] + content.lstrip()[len("[VOICE]") :].lstrip()
        content, _, _ = parse_response_tags(content)
        return PreparedPresentation(
            content_text=content.strip(),
            actions=self.actions,
            recovery_actions=self.recovery_actions,
            failure_actions=self.failure_actions,
            display_prefix=self.display_prefix,
            footer=self.footer,
            long_read_title=self.long_read_title,
        )


ActionBuilder = Callable[
    [str, str | None, list[dict[str, str]]],
    InlineKeyboardMarkup | None,
]


@dataclass(frozen=True, slots=True)
class ChatPresentation:
    action_builder: ActionBuilder
    recovery_actions: InlineKeyboardMarkup | None = None
    failure_actions: InlineKeyboardMarkup | None = None
    display_prefix: str = ""
    footer: str = ""
    long_read_title: str = "Ответ ИИ"

    def prepare(self, facts: PresentationFacts) -> PreparedPresentation:
        content = strip_hallucinated_tool_trace(facts.raw_content)
        if content.lstrip().startswith("[VOICE]"):
            leading = len(content) - len(content.lstrip())
            content = content[:leading] + content.lstrip()[len("[VOICE]") :].lstrip()
        content, intent, suggestions = parse_response_tags(content)
        content = content.strip()
        return PreparedPresentation(
            content_text=content,
            actions=self.action_builder(content, intent, suggestions),
            recovery_actions=self.recovery_actions,
            failure_actions=self.failure_actions,
            display_prefix=self.display_prefix,
            footer=self.footer,
            long_read_title=self.long_read_title,
        )


__all__ = [
    "ChatPresentation",
    "FixedPresentation",
    "PreparedPresentation",
    "PresentationFacts",
    "TelegramPresentation",
]
