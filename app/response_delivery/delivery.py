"""Small handler-facing façade for streamed and already-completed responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.errors import ErrorCode, user_message_for_error_code
from app.providers.stream_types import GenerationRequest, StreamCompleted
from app.response_delivery.coordinator import AIStreamCoordinator
from app.response_delivery.outcomes import (
    CompleteDelivery,
    DeferredDelivery,
    FailedDelivery,
    TelegramResponseOutcome,
)
from app.response_delivery.presentation import PresentationFacts, TelegramPresentation
from app.response_delivery.renderer import (
    TelegramBotTransport,
    TelegramMessageTransport,
    TelegramRenderer,
)


@dataclass(frozen=True, slots=True)
class TelegramTarget:
    placeholder_message: Any | None = None
    bot: Any | None = None
    chat_id: int | None = None


@dataclass(frozen=True, slots=True)
class CompletedResponse:
    content_text: str
    completion: StreamCompleted | None = None
    voice_requested: bool = False


@dataclass(frozen=True, slots=True)
class GenerationFailure:
    error_code: ErrorCode


@dataclass(frozen=True, slots=True)
class DeferredGeneration:
    task_id: str


class TelegramResponseDelivery:
    def __init__(self, router=None, *, renderer_factory=TelegramRenderer):
        self._router = router
        self._renderer_factory = renderer_factory

    @staticmethod
    def _transport(target: TelegramTarget):
        if target.placeholder_message is None:
            if target.bot is None or target.chat_id is None:
                raise ValueError("Send-only TelegramTarget requires bot and chat_id")
            return TelegramBotTransport(target.bot, chat_id=target.chat_id)
        return TelegramMessageTransport(
            target.placeholder_message,
            bot=target.bot,
            chat_id=target.chat_id,
        )

    def _renderer(self, target: TelegramTarget) -> TelegramRenderer:
        return self._renderer_factory(self._transport(target))

    async def stream(
        self,
        target: TelegramTarget,
        generation: GenerationRequest,
        *,
        presentation: TelegramPresentation,
    ) -> TelegramResponseOutcome:
        router = self._router
        if router is None:
            from app.providers import get_provider_router

            router = get_provider_router()
        session = self._renderer(target).open()

        async def _stop_heartbeat() -> None:
            if target.placeholder_message is None:
                return
            from app.utils.heartbeat import stop_heartbeat

            stop_heartbeat(target.placeholder_message.message_id)

        coordinator = AIStreamCoordinator(
            router,
            session,
            on_first_text=_stop_heartbeat,
        )
        return await coordinator.run(generation, presentation)

    async def deliver(
        self,
        target: TelegramTarget,
        completed: CompletedResponse | GenerationFailure | DeferredGeneration,
        *,
        presentation: TelegramPresentation,
    ) -> TelegramResponseOutcome:
        session = self._renderer(target).open()

        if isinstance(completed, CompletedResponse):
            prepared = presentation.prepare(
                PresentationFacts(
                    raw_content=completed.content_text,
                    terminal=completed.completion,
                    voice_requested=completed.voice_requested,
                )
            )
            content = prepared.content_text
            displayed = prepared.display_prefix + content
            if prepared.footer:
                displayed += (
                    prepared.footer
                    if prepared.footer.startswith("\n")
                    else f"\n\n{prepared.footer}"
                )
            receipt = await session.finalize(
                displayed_text=displayed,
                title=prepared.long_read_title,
                actions=prepared.actions,
            )
            return CompleteDelivery(
                content_text=content,
                displayed_text=displayed,
                completion=completed.completion,
                voice_requested=completed.voice_requested,
                receipt=receipt,
            )

        prepared = presentation.prepare(
            PresentationFacts(
                raw_content="",
                terminal=None,
                voice_requested=False,
            )
        )
        if isinstance(completed, GenerationFailure):
            displayed = user_message_for_error_code(completed.error_code)
            receipt = await session.finalize(
                displayed_text=displayed,
                title=prepared.long_read_title,
                actions=prepared.failure_actions,
            )
            return FailedDelivery(
                error_code=completed.error_code,
                displayed_text=displayed,
                receipt=receipt,
            )

        displayed = (
            "⏳ Серверы AI временно перегружены. "
            "Я отправлю ответ, как только они освободятся."
        )
        receipt = await session.finalize(
            displayed_text=displayed,
            title=prepared.long_read_title,
            actions=prepared.failure_actions,
        )
        return DeferredDelivery(
            task_id=completed.task_id,
            displayed_text=displayed,
            receipt=receipt,
        )


_delivery: TelegramResponseDelivery | None = None


def get_telegram_response_delivery() -> TelegramResponseDelivery:
    global _delivery
    if _delivery is None:
        _delivery = TelegramResponseDelivery()
    return _delivery


__all__ = [
    "CompletedResponse",
    "DeferredGeneration",
    "GenerationFailure",
    "TelegramResponseDelivery",
    "TelegramTarget",
    "get_telegram_response_delivery",
]
