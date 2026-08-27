"""Coordinator tests for typed generation-to-Telegram outcomes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.errors import ErrorCode
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
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
    TokenUsage,
)
from app.response_delivery.coordinator import AIStreamCoordinator
from app.response_delivery.outcomes import (
    CompleteDelivery,
    DeferredDelivery,
    FailedDelivery,
    PartialDelivery,
)
from app.response_delivery.presentation import FixedPresentation
from app.response_delivery.renderer import (
    DeliveryKind,
    DeliveryReceipt,
    TelegramMessageRef,
)


def _request() -> GenerationRequest:
    return GenerationRequest(
        models=("gemini-3.5-flash",),
        turns=(PromptTurn(PromptRole.USER, (TextPart("hello"),)),),
        scope=RequestScope(user_id=7, chat_id=10),
    )


def _completed(raw: str = "STOP") -> StreamCompleted:
    return StreamCompleted(
        finish_reason=FinishReason.from_raw(raw),
        usage=TokenUsage(total=12),
        grounding=GroundingReport(),
        route=RouteUsed(
            provider=ProviderKind.GEMINI,
            requested_model="gemini-3.5-flash",
            actual_model="gemini-3.5-flash",
        ),
    )


class FakeRouter:
    def __init__(self, events):
        self.events = events
        self.closed = False

    async def stream(self, request):
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


class FakeSession:
    def __init__(self):
        self.appended: list[str] = []
        self.finalize_calls: list[dict] = []
        self.status_calls: list[tuple[str, object]] = []

    async def append(self, text):
        self.appended.append(text)

    async def show_status(self, text, actions=None):
        self.status_calls.append((text, actions))

    async def finalize(self, *, displayed_text, title, actions):
        self.finalize_calls.append({"displayed_text": displayed_text, "title": title, "actions": actions})
        return DeliveryReceipt(
            kind=DeliveryKind.MESSAGE,
            message_ids=(1,),
            final_message=TelegramMessageRef(chat_id=10, message_id=1),
        )


def _presentation(**kwargs):
    return FixedPresentation(
        actions=kwargs.get("actions"),
        recovery_actions=kwargs.get("recovery_actions"),
        failure_actions=kwargs.get("failure_actions"),
        footer=kwargs.get("footer", ""),
        long_read_title="Answer",
    )


@pytest.mark.asyncio
async def test_split_voice_prefix_is_hidden_and_content_is_separate_from_footer():
    router = FakeRouter([TextDelta("[VO"), TextDelta("ICE] Hello"), _completed()])
    session = FakeSession()
    coordinator = AIStreamCoordinator(router, session, feedback_delay=60)

    outcome = await coordinator.run(
        _request(),
        _presentation(footer="memory footer"),
    )

    assert isinstance(outcome, CompleteDelivery)
    assert outcome.content_text == "Hello"
    assert outcome.displayed_text == "Hello\n\nmemory footer"
    assert outcome.voice_requested is True
    assert "".join(session.appended) == "Hello"
    assert session.finalize_calls[0]["displayed_text"] == outcome.displayed_text
    assert router.closed is True


@pytest.mark.asyncio
async def test_model_limit_is_partial_with_notice_and_normal_actions():
    actions = InlineKeyboardMarkup([[InlineKeyboardButton("Action", callback_data="action")]])
    session = FakeSession()
    coordinator = AIStreamCoordinator(
        FakeRouter([TextDelta("Partial"), _completed("MAX_TOKENS")]),
        session,
        feedback_delay=60,
    )

    outcome = await coordinator.run(_request(), _presentation(actions=actions))

    assert isinstance(outcome, PartialDelivery)
    assert "ограничения длины" in outcome.displayed_text
    assert session.finalize_calls[0]["actions"] is actions


@pytest.mark.asyncio
async def test_transport_partial_uses_recovery_actions_and_omits_normal_footer():
    recovery = InlineKeyboardMarkup([[InlineKeyboardButton("Continue", callback_data="continue")]])
    failure = StreamFailed(
        code=ErrorCode.TIMEOUT,
        phase=FailurePhase.AFTER_TEXT,
        retry=RetryDisposition.DO_NOT_RETRY,
        key=KeyDisposition.UNCHANGED,
        diagnostic="timeout",
    )
    session = FakeSession()
    coordinator = AIStreamCoordinator(
        FakeRouter([TextDelta("Partial"), failure]),
        session,
        feedback_delay=60,
    )

    outcome = await coordinator.run(
        _request(),
        _presentation(footer="must not appear", recovery_actions=recovery),
    )

    assert isinstance(outcome, PartialDelivery)
    assert outcome.content_text == "Partial"
    assert "must not appear" not in outcome.displayed_text
    assert "прерван" in outcome.displayed_text
    assert session.finalize_calls[0]["actions"] is recovery


@pytest.mark.asyncio
async def test_pre_text_failure_is_rendered_without_becoming_content():
    failure = StreamFailed(
        code=ErrorCode.KEYS_EXHAUSTED,
        phase=FailurePhase.BEFORE_TEXT,
        retry=RetryDisposition.RETRY_LATER,
        key=KeyDisposition.EXHAUSTED,
        diagnostic="all keys exhausted",
    )
    session = FakeSession()
    coordinator = AIStreamCoordinator(FakeRouter([failure]), session, feedback_delay=60)

    outcome = await coordinator.run(_request(), _presentation())

    assert isinstance(outcome, FailedDelivery)
    assert outcome.content_text == ""
    assert "перегружен" in outcome.displayed_text.lower()
    assert "ключ" not in outcome.displayed_text.lower()
    assert session.appended == []
    assert len(session.finalize_calls) == 1


@pytest.mark.asyncio
async def test_deferred_is_a_distinct_outcome_not_failure_text():
    session = FakeSession()
    coordinator = AIStreamCoordinator(
        FakeRouter([StreamDeferred("task-1")]),
        session,
        feedback_delay=60,
    )

    outcome = await coordinator.run(_request(), _presentation())

    assert isinstance(outcome, DeferredDelivery)
    assert outcome.task_id == "task-1"
    assert "отправлю ответ" in outcome.displayed_text.lower()


@pytest.mark.asyncio
async def test_cancellation_rethrows_after_request_lifecycle_cleanup():
    cleanup = MagicMock()
    delayed_started = asyncio.Event()

    class BlockingRouter:
        closed = False

        async def stream(self, request):
            try:
                await asyncio.Event().wait()
                if False:
                    yield TextDelta("never")
            finally:
                self.closed = True

    router = BlockingRouter()
    coordinator = AIStreamCoordinator(
        router,
        FakeSession(),
        mark_network_waiting=MagicMock(),
        mark_network_alive=MagicMock(),
        clear_network_stall=cleanup,
        delayed_feedback=lambda: delayed_started.set(),
        feedback_delay=0,
    )
    task = asyncio.create_task(coordinator.run(_request(), _presentation()))
    await delayed_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    cleanup.assert_called_once_with(7)
    assert router.closed is True


@pytest.mark.asyncio
async def test_default_delayed_status_keeps_cancel_generation_action():
    status_visible = asyncio.Event()

    class StatusSession(FakeSession):
        async def show_status(self, text, actions=None):
            await super().show_status(text, actions)
            status_visible.set()

    class BlockingRouter:
        async def stream(self, request):
            await asyncio.Event().wait()
            if False:
                yield TextDelta("never")

    session = StatusSession()
    coordinator = AIStreamCoordinator(
        BlockingRouter(),
        session,
        feedback_delay=0,
    )
    task = asyncio.create_task(coordinator.run(_request(), _presentation()))
    await status_visible.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    actions = session.status_calls[0][1]
    assert actions.inline_keyboard[0][0].callback_data == "cancel_generation"
