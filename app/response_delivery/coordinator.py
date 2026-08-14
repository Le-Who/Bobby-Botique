"""Request-scoped coordination of typed provider events and Telegram rendering."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.errors import ErrorCode, user_message_for_error_code
from app.providers.stream_types import (
    FailurePhase,
    FinishKind,
    GenerationRequest,
    ProviderStreamProtocolError,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    is_terminal_event,
)
from app.response_delivery.outcomes import (
    CompleteDelivery,
    DeferredDelivery,
    FailedDelivery,
    PartialDelivery,
    TelegramResponseOutcome,
)
from app.response_delivery.presentation import PresentationFacts, TelegramPresentation


class ProviderRouterProtocol(Protocol):
    def stream(self, request: GenerationRequest): ...


class RendererSessionProtocol(Protocol):
    async def append(self, text: str) -> None: ...

    async def show_status(self, text: str, actions=None) -> None: ...

    async def finalize(self, *, displayed_text: str, title: str, actions): ...


LifecycleCallback = Callable[..., Any]


def _mark_network_waiting(user_id: int) -> None:
    from app import state

    state.mark_network_waiting(user_id)


def _mark_network_alive(user_id: int) -> None:
    from app import state

    state.mark_network_alive(user_id)


def _clear_network_stall(user_id: int) -> None:
    from app import state

    state.clear_network_stall(user_id)


async def _invoke(callback: LifecycleCallback | None, *args) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


def _append_block(text: str, block: str) -> str:
    if not block:
        return text
    if block.startswith("\n"):
        return text + block
    return f"{text}\n\n{block}"


class AIStreamCoordinator:
    _VOICE_TAG = "[VOICE]"

    def __init__(
        self,
        router: ProviderRouterProtocol,
        renderer_session: RendererSessionProtocol,
        *,
        mark_network_waiting: LifecycleCallback = _mark_network_waiting,
        mark_network_alive: LifecycleCallback = _mark_network_alive,
        clear_network_stall: LifecycleCallback = _clear_network_stall,
        on_first_text: LifecycleCallback | None = None,
        delayed_feedback: LifecycleCallback | None = None,
        feedback_delay: float = 5.0,
    ) -> None:
        self._router = router
        self._session = renderer_session
        self._mark_waiting = mark_network_waiting
        self._mark_alive = mark_network_alive
        self._clear_stall = clear_network_stall
        self._on_first_text = on_first_text
        self._delayed_feedback = delayed_feedback
        self._feedback_delay = feedback_delay

    async def run(
        self,
        request: GenerationRequest,
        presentation: TelegramPresentation,
    ) -> TelegramResponseOutcome:
        user_id = request.scope.user_id
        first_visible = False
        prefix_decided = False
        prefix_buffer = ""
        voice_requested = False
        raw_parts: list[str] = []
        terminal = None
        iterator = self._router.stream(request)

        async def _first_text() -> None:
            nonlocal first_visible
            if first_visible:
                return
            first_visible = True
            if feedback_task and not feedback_task.done():
                feedback_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feedback_task
            if user_id is not None:
                await _invoke(self._mark_alive, user_id)
            await _invoke(self._on_first_text)

        async def _append_visible(text: str) -> None:
            if not text:
                return
            if text.strip():
                await _first_text()
            raw_parts.append(text)
            await self._session.append(text)

        async def _feedback() -> None:
            await asyncio.sleep(self._feedback_delay)
            if first_visible:
                return
            if self._delayed_feedback is not None:
                await _invoke(self._delayed_feedback)
            else:
                await self._session.show_status(
                    "⏳ Запрос в обработке: высокая нагрузка на сервера...",
                    InlineKeyboardMarkup(
                        [[InlineKeyboardButton("❌ Отменить", callback_data="cancel_generation")]]
                    ),
                )

        if user_id is not None:
            await _invoke(self._mark_waiting, user_id)
        feedback_task = asyncio.create_task(_feedback())

        try:
            async for event in iterator:
                if terminal is not None:
                    raise ProviderStreamProtocolError(
                        "Router emitted an event after its terminal event"
                    )
                if isinstance(event, TextDelta):
                    if prefix_decided:
                        await _append_visible(event.text)
                        continue

                    prefix_buffer += event.text
                    stripped = prefix_buffer.lstrip()
                    if self._VOICE_TAG.startswith(stripped) and len(stripped) < len(self._VOICE_TAG):
                        continue
                    if stripped.startswith(self._VOICE_TAG):
                        voice_requested = True
                        prefix_decided = True
                        visible = stripped[len(self._VOICE_TAG) :].lstrip()
                        await _append_visible(visible)
                    else:
                        prefix_decided = True
                        await _append_visible(prefix_buffer)
                    prefix_buffer = ""
                    continue

                if not is_terminal_event(event):
                    raise ProviderStreamProtocolError(
                        f"Unsupported router event: {type(event).__name__}"
                    )
                terminal = event

            if terminal is None:
                raise ProviderStreamProtocolError("Router ended without a terminal event")

            if not prefix_decided and prefix_buffer:
                await _append_visible(prefix_buffer)

            facts = PresentationFacts(
                raw_content="".join(raw_parts),
                terminal=terminal,
                voice_requested=voice_requested,
            )
            prepared = presentation.prepare(facts)

            if isinstance(terminal, StreamDeferred):
                displayed = (
                    "⏳ Серверы AI временно перегружены. "
                    "Я отправлю ответ, как только они освободятся."
                )
                receipt = await self._session.finalize(
                    displayed_text=displayed,
                    title=prepared.long_read_title,
                    actions=prepared.failure_actions,
                )
                return DeferredDelivery(
                    task_id=terminal.task_id,
                    displayed_text=displayed,
                    receipt=receipt,
                )

            if isinstance(terminal, StreamFailed) and terminal.phase is FailurePhase.BEFORE_TEXT:
                displayed = user_message_for_error_code(terminal.code)
                receipt = await self._session.finalize(
                    displayed_text=displayed,
                    title=prepared.long_read_title,
                    actions=prepared.failure_actions,
                )
                return FailedDelivery(
                    error_code=terminal.code,
                    displayed_text=displayed,
                    receipt=receipt,
                )

            content = prepared.content_text
            if not content:
                displayed = user_message_for_error_code(ErrorCode.EMPTY_RESPONSE)
                receipt = await self._session.finalize(
                    displayed_text=displayed,
                    title=prepared.long_read_title,
                    actions=prepared.failure_actions,
                )
                return FailedDelivery(
                    error_code=ErrorCode.EMPTY_RESPONSE,
                    displayed_text=displayed,
                    receipt=receipt,
                )

            if isinstance(terminal, StreamFailed):
                notice = (
                    "⚠️ _(ответ был прерван по таймауту)_"
                    if terminal.code is ErrorCode.TIMEOUT
                    else "⚠️ _(ответ был прерван из-за ошибки сервера)_"
                )
                displayed = prepared.display_prefix + _append_block(content, notice)
                receipt = await self._session.finalize(
                    displayed_text=displayed,
                    title=prepared.long_read_title,
                    actions=prepared.recovery_actions,
                )
                return PartialDelivery(
                    content_text=content,
                    displayed_text=displayed,
                    terminal=terminal,
                    voice_requested=voice_requested,
                    receipt=receipt,
                )

            assert isinstance(terminal, StreamCompleted)
            displayed = prepared.display_prefix + _append_block(content, prepared.footer)
            notice = ""
            if terminal.finish_reason.kind is FinishKind.MAX_TOKENS:
                notice = "⚠️ _Ответ был обрезан из-за ограничения длины._"
            elif terminal.finish_reason.kind in {FinishKind.SAFETY, FinishKind.RECITATION}:
                notice = "⚠️ _Ответ был прерван фильтром безопасности._"
            if notice:
                displayed = _append_block(displayed, notice)

            receipt = await self._session.finalize(
                displayed_text=displayed,
                title=prepared.long_read_title,
                actions=prepared.actions,
            )
            if notice:
                return PartialDelivery(
                    content_text=content,
                    displayed_text=displayed,
                    terminal=terminal,
                    voice_requested=voice_requested,
                    receipt=receipt,
                )
            return CompleteDelivery(
                content_text=content,
                displayed_text=displayed,
                completion=terminal,
                voice_requested=voice_requested,
                receipt=receipt,
            )
        except asyncio.CancelledError:
            raise
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await aclose()
            if not feedback_task.done():
                feedback_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feedback_task
            if user_id is not None:
                await _invoke(self._clear_stall, user_id)


__all__ = ["AIStreamCoordinator"]
