"""Deterministic malformed two-key race contracts through the real router."""

import asyncio
from types import SimpleNamespace

import pytest

from app.errors import ErrorCode
from app.providers.router import ProviderRouter
from app.providers.stream_types import (
    FailurePhase,
    FinishReason,
    GenerationRequest,
    GroundingReport,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    ProviderKind,
    ProviderStreamProtocolError,
    RequestScope,
    RetryDisposition,
    RouteUsed,
    StreamCompleted,
    StreamDeferred,
    StreamFailed,
    TextDelta,
    TextPart,
    TokenUsage,
    Workload,
    is_terminal_event,
)
from app.response_delivery.coordinator import AIStreamCoordinator
from app.response_delivery.outcomes import CompleteDelivery, PartialDelivery
from app.response_delivery.presentation import FixedPresentation
from app.response_delivery.renderer import DeliveryKind, DeliveryReceipt, TelegramMessageRef

MODEL = "gemini-3.5-flash"


def completion():
    return StreamCompleted(
        finish_reason=FinishReason.from_raw("STOP"),
        usage=TokenUsage(total=23),
        grounding=GroundingReport(),
        route=RouteUsed(provider=ProviderKind.GEMINI, requested_model=MODEL, actual_model=MODEL),
    )


def failure(phase):
    return StreamFailed(
        code=ErrorCode.NETWORK,
        phase=phase,
        retry=RetryDisposition.DO_NOT_RETRY,
        key=KeyDisposition.UNCHANGED,
        diagnostic="synthetic stream fault",
    )


class TwoKeyBoundary:
    def __init__(self):
        self.reserved = []
        self.successes = []

    async def resolve_exact_ai_request(self, model, *, excluded_key_hashes):
        for key in ("winner", "loser"):
            if key not in excluded_key_hashes:
                return {"api_key": key, "key_hash": key}, model, None
        return None, None, "all_exhausted"

    async def reserve_key_usage(self, key, model, _provider):
        self.reserved.append((key, model))
        return True

    async def record_success(self, key, model):
        self.successes.append((key, model))


class ScriptedProvider:
    provider_name = "gemini"

    def __init__(self, key, script, loser_started):
        self.key = key
        self.script = script
        self.loser_started = loser_started
        self.task = None
        self.closed = False

    async def stream(self, request, *, model_name):
        self.task = asyncio.current_task()
        try:
            if self.key == "loser":
                self.loser_started.set()
                await asyncio.Event().wait()
                yield TextDelta("Loser must stay invisible")
            else:
                await self.loser_started.wait()
                for event in self.script:
                    yield event
        finally:
            # Closure must be awaited by the producer, not left to asyncgen GC.
            await asyncio.sleep(0)
            self.closed = True


def install_race(monkeypatch, script):
    boundary = TwoKeyBoundary()
    started = asyncio.Event()
    providers = {key: ScriptedProvider(key, script, started) for key in ("winner", "loser")}
    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: boundary)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: boundary)
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda _model, key: providers[key])
    request = GenerationRequest(
        models=(MODEL,),
        turns=(PromptTurn(PromptRole.USER, (TextPart("question"),)),),
        scope=RequestScope(user_id=71, chat_id=71),
        workload=Workload.INLINE,
        allow_model_fallback=False,
    )
    return ProviderRouter(), request, providers, boundary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,diagnostic",
    [
        ([], "without terminal event"),
        ([completion(), TextDelta("late")], "after terminal"),
        ([failure(FailurePhase.BEFORE_TEXT)], "BEFORE_TEXT after text"),
        ([StreamDeferred(task_id="synthetic-deferred")], "deferred after visible text"),
        ([object()], "Unsupported provider event"),
    ],
    ids=["missing-terminal", "post-terminal", "wrong-phase", "deferred-after-text", "unsupported-event"],
)
async def test_malformed_race_winner_raises_without_terminal_or_mixed_text_and_awaits_producers(
    monkeypatch, suffix, diagnostic
):
    # Returning early at a provider terminal, accepting a wrong phase, or orphaning
    # an observed iterator would lose protocol errors/closure or expose late text.
    router, request, providers, boundary = install_race(monkeypatch, [TextDelta("Winner"), *suffix])
    events = []
    with pytest.raises(ProviderStreamProtocolError, match=diagnostic):
        async for event in router.stream(request):
            events.append(event)

    assert events == [TextDelta("Winner")]
    assert sum(is_terminal_event(event) for event in events) == 0
    assert boundary.successes == [("winner", MODEL)]
    assert boundary.reserved == [("winner", MODEL), ("loser", MODEL)]
    for provider in providers.values():
        assert provider.task.done()
        assert provider.closed is True


@pytest.mark.asyncio
async def test_loser_exception_before_winner_is_explicit_and_both_producers_are_awaited(monkeypatch):
    # A lane error preceding selection must remain explicit, with no partial success
    # emitted from the other lane while ownership cleanup is still outstanding.
    router, request, providers, _boundary = install_race(monkeypatch, [TextDelta("Winner"), completion()])
    winner_waiting = asyncio.Event()

    class FaultingProvider:
        provider_name = "gemini"

        def __init__(self, key):
            self.key = key

        async def stream(self, request, *, model_name):
            providers[self.key].task = asyncio.current_task()
            try:
                if self.key == "winner":
                    winner_waiting.set()
                    await asyncio.Event().wait()
                    yield TextDelta("Winner must stay invisible")
                else:
                    await winner_waiting.wait()
                    raise RuntimeError("synthetic losing lane exception")
            finally:
                await asyncio.sleep(0)
                providers[self.key].closed = True

    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda _model, key: FaultingProvider(key))
    events = []
    with pytest.raises(RuntimeError, match="synthetic losing lane exception"):
        async for event in router.stream(request):
            events.append(event)
    assert events == []
    assert all(provider.task.done() and provider.closed for provider in providers.values())


@pytest.mark.asyncio
async def test_loser_error_during_cancellation_after_selection_keeps_winner_terminal(monkeypatch):
    # A losing lane's exception after selection must not replace the selected
    # answer, metadata, or single terminal, even if it faults while being closed.
    router, request, providers, _boundary = install_race(monkeypatch, [TextDelta("Winner"), completion()])
    loser_faulted = asyncio.Event()

    class FaultOnCancel:
        provider_name = "gemini"

        async def stream(self, request, *, model_name):
            loser = providers["loser"]
            loser.task = asyncio.current_task()
            try:
                loser.loser_started.set()
                await asyncio.Event().wait()
                yield TextDelta("Invisible loser")
            except asyncio.CancelledError:
                loser_faulted.set()
                raise RuntimeError("synthetic loser closure error") from None
            finally:
                await asyncio.sleep(0)
                loser.closed = True

    monkeypatch.setattr(
        "app.providers.base.get_provider_for_model",
        lambda _model, key: FaultOnCancel() if key == "loser" else providers[key],
    )
    events = [event async for event in router.stream(request)]
    assert loser_faulted.is_set()
    assert events == [TextDelta("Winner"), completion()]
    assert sum(is_terminal_event(event) for event in events) == 1
    assert all(provider.task.done() and provider.closed for provider in providers.values())


class RendererBoundary:
    def __init__(self):
        self.appended = []
        self.finalized = []

    async def append(self, text):
        self.appended.append(text)

    async def show_status(self, text, actions=None):
        raise AssertionError("Delayed feedback must be cancelled before delivery returns")

    async def finalize(self, **kwargs):
        self.finalized.append(kwargs)
        return DeliveryReceipt(
            kind=DeliveryKind.MESSAGE, message_ids=(1,), final_message=TelegramMessageRef(chat_id=71, message_id=1)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal,outcome_type",
    [(completion(), CompleteDelivery), (failure(FailurePhase.AFTER_TEXT), PartialDelivery)],
    ids=["completed", "partial"],
)
async def test_valid_race_has_one_typed_terminal_and_coordinator_exact_outcome(monkeypatch, terminal, outcome_type):
    # Winner metadata and partial classification must survive actual router+delivery,
    # and all producer/feedback tasks must be finished at the public result boundary.
    router, request, providers, _boundary = install_race(monkeypatch, [TextDelta("Winner"), terminal])
    renderer = RendererBoundary()
    events = []
    owned_feedback = []
    create_task = asyncio.create_task

    def track_task(coro, **kwargs):
        task = create_task(coro, **kwargs)
        if coro.cr_code.co_name == "_feedback":
            owned_feedback.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", track_task)

    async def recorded_stream(req):
        async for event in router.stream(req):
            events.append(event)
            yield event

    coordinator = AIStreamCoordinator(
        SimpleNamespace(stream=recorded_stream),
        renderer,
        mark_network_waiting=lambda _: None,
        mark_network_alive=lambda _: None,
        clear_network_stall=lambda _: None,
        feedback_delay=60,
    )
    outcome = await coordinator.run(request, FixedPresentation(long_read_title="Answer"))
    assert type(outcome) is outcome_type
    assert outcome.content_text == "Winner"
    assert events == [TextDelta("Winner"), terminal]
    assert sum(is_terminal_event(event) for event in events) == 1
    assert renderer.appended == ["Winner"]
    assert len(renderer.finalized) == 1
    assert all(provider.task.done() and provider.closed for provider in providers.values())
    assert len(owned_feedback) == 1
    assert owned_feedback[0].done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,diagnostic",
    [
        ([], "without terminal event"),
        ([completion(), TextDelta("late")], "after terminal"),
        ([failure(FailurePhase.BEFORE_TEXT)], "BEFORE_TEXT after text"),
        ([StreamDeferred(task_id="synthetic-deferred")], "deferred after emitting visible text"),
        ([object()], "Unsupported provider event"),
    ],
    ids=["missing-terminal", "post-terminal", "wrong-phase", "deferred-after-text", "unsupported-event"],
)
async def test_malformed_hedged_model_winner_preserves_protocol_error_and_awaits_lanes(monkeypatch, suffix, diagnostic):
    # Outer model hedging must not re-cancel a loser already running async cleanup
    # or hide a protocol error coming from the selected single-key lane.
    router, _request, providers, boundary = install_race(monkeypatch, [TextDelta("Winner"), *suffix])
    backup = "gemini-3.1-flash-lite"

    async def resolve_exact(model, *, excluded_key_hashes):
        key = "winner" if model == MODEL else "loser"
        if key in excluded_key_hashes:
            return None, None, "all_exhausted"
        return {"api_key": key, "key_hash": key}, model, None

    monkeypatch.setattr(boundary, "resolve_exact_ai_request", resolve_exact)
    monkeypatch.setattr("app.providers.gemini.is_vertex_client_available", lambda: False)
    monkeypatch.setattr("app.providers.router.MODEL_HEDGE_DELAY_SECONDS", 0)
    request = GenerationRequest(
        models=(MODEL, backup),
        turns=(PromptTurn(PromptRole.USER, (TextPart("question"),)),),
        workload=Workload.INTERACTIVE,
        allow_model_fallback=False,
        allow_deferred=False,
    )
    events = []
    with pytest.raises(ProviderStreamProtocolError, match=diagnostic):
        async for event in router.stream(request):
            events.append(event)
    assert events == [TextDelta("Winner")]
    assert all(provider.task.done() and provider.closed for provider in providers.values())
