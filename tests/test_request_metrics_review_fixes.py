"""Regression contracts for semantic request ownership and visible Telegram writes."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.errors import ErrorCode, UserLimitExceededError
from app.observability import request_metrics
from app.observability.metrics import OperationalMetrics
from app.providers.stream_types import (
    FailurePhase,
    GenerationRequest,
    KeyDisposition,
    PromptRole,
    PromptTurn,
    RetryDisposition,
    StreamFailed,
    TextDelta,
    TextPart,
)
from app.response_delivery.delivery import (
    CompletedResponse,
    GenerationFailure,
    TelegramResponseDelivery,
    TelegramTarget,
)
from app.response_delivery.outcomes import PartialDelivery
from app.response_delivery.presentation import FixedPresentation
from app.response_delivery.renderer import TelegramDeliveryError, TelegramMessageRef, TelegramRenderer


class RecordingTransport:
    def __init__(self):
        self.current_ref = TelegramMessageRef(chat_id=1, message_id=2)
        self.bodies = []

    async def edit(self, text, *, parse_mode, reply_markup):
        self.bodies.append(text)

    async def send(self, text, *, parse_mode, reply_markup):
        self.bodies.append(text)
        self.current_ref = TelegramMessageRef(chat_id=1, message_id=self.current_ref.message_id + 1)
        return self.current_ref


class LocalDelivery(TelegramResponseDelivery):
    """Keep the delivery owner and renderer real; replace only Telegram I/O."""

    def __init__(self, transport, *, router=None, **renderer_options):
        super().__init__(router)
        self.renderer = TelegramRenderer(
            transport, webapp_base_url="", telegraph_publication_enabled=False, **renderer_options
        )

    def _renderer(self, target):
        return self.renderer

    @staticmethod
    async def _retire_heartbeat(target):
        pass


@pytest.mark.asyncio
async def test_partial_delivery_survives_legacy_failure_and_accounting_owners(monkeypatch):
    from app import metrics
    from app.utils.metrics_middleware import MetricsMiddleware, mark_operation_failed

    class Router:
        async def stream(self, request):
            yield TextDelta("Synthetic partial answer")
            yield StreamFailed(
                ErrorCode.TIMEOUT,
                FailurePhase.AFTER_TEXT,
                RetryDisposition.DO_NOT_RETRY,
                KeyDisposition.UNCHANGED,
                "synthetic timeout",
            )

    collector = metrics.MetricsCollector()
    monkeypatch.setattr(metrics, "metrics_collector", collector)
    operational = OperationalMetrics()
    monkeypatch.setattr(request_metrics, "emit", lambda name, **fields: operational.observe(name, fields))
    delivery = LocalDelivery(RecordingTransport(), router=Router())
    request = GenerationRequest(
        models=("synthetic",), turns=(PromptTurn(PromptRole.USER, (TextPart("synthetic request"),)),)
    )
    async with MetricsMiddleware("chat", user_id=77):
        outcome = await delivery.stream(TelegramTarget(), request, presentation=FixedPresentation())
        assert isinstance(outcome, PartialDelivery)
        mark_operation_failed()
    collector._drain_events()
    assert collector.metrics.request_count == 1
    assert collector.metrics.error_count == 1
    assert operational.snapshot()["request_outcomes"] == {"partial": 1}


@pytest.mark.asyncio
async def test_handled_admission_rejection_survives_legacy_accounting(monkeypatch):
    from app import metrics
    from app.adapters.concurrency import GlobalLLMSemaphore
    from app.utils.metrics_middleware import MetricsMiddleware, mark_operation_failed

    collector = metrics.MetricsCollector()
    monkeypatch.setattr(metrics, "metrics_collector", collector)
    operational = OperationalMetrics()
    monkeypatch.setattr(request_metrics, "emit", lambda name, **fields: operational.observe(name, fields))
    semaphore = GlobalLLMSemaphore(1)
    monkeypatch.setattr(semaphore, "_acquire", AsyncMock(side_effect=UserLimitExceededError("synthetic capacity")))
    async with MetricsMiddleware("chat", user_id=77):
        try:
            async with semaphore:
                pytest.fail("Rejected work cannot run")
        except UserLimitExceededError:
            mark_operation_failed()
    collector._drain_events()
    assert collector.metrics.error_count == 1
    assert operational.snapshot()["request_outcomes"] == {"rejected": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("progressive", [True, False])
async def test_normalized_tool_trace_cursor_and_error_are_never_model_visibility(progressive):
    transport = RecordingTransport()
    session = TelegramRenderer(transport, min_chunk_size=1 if progressive else 10000, debounce_seconds=0).open()
    observation = request_metrics.RequestObservation()
    with observation.bind():
        await session.append("[tool_code]\nimport google_search\ngoogle_search.search(query)\n")
        assert observation.visible is False
        await session.finalize(displayed_text="Synthetic failure notice", title="Answer", actions=None)
        assert observation.visible is False


@pytest.mark.asyncio
@pytest.mark.parametrize("later_outcome", ["success", "failure", "cancelled"])
async def test_completed_split_visibility_is_first_successful_part_even_if_later_send_fails(monkeypatch, later_outcome):
    clock = [0.0]
    operational = OperationalMetrics(clock=lambda: clock[0])
    monkeypatch.setattr(request_metrics, "emit", lambda name, **fields: operational.observe(name, fields))
    observation = request_metrics.RequestObservation(clock=lambda: clock[0])
    visibility_before_later_parts = []

    class Transport(RecordingTransport):
        async def edit(self, text, **kwargs):
            clock[0] += 1
            await super().edit(text, **kwargs)

        async def send(self, text, **kwargs):
            visibility_before_later_parts.append(observation.visible)
            clock[0] += 2
            if later_outcome == "failure":
                raise RuntimeError("synthetic later split failure")
            if later_outcome == "cancelled":
                raise asyncio.CancelledError
            return await super().send(text, **kwargs)

    delivery = LocalDelivery(Transport(), message_limit=100, private_content=True)
    with observation.bind():
        operation = delivery.deliver(
            TelegramTarget(), CompletedResponse("Synthetic model text. " * 20), presentation=FixedPresentation()
        )
        if later_outcome == "success":
            await operation
        else:
            with pytest.raises(TelegramDeliveryError if later_outcome == "failure" else asyncio.CancelledError):
                await operation
        observation.finish()
    assert visibility_before_later_parts and all(visibility_before_later_parts)
    visible = [row for row in operational.snapshot()["stages"] if row["stage"] == "first_visible_text"]
    assert len(visible) == 1
    assert visible[0]["count"] == 1
    assert visible[0]["sum_seconds"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completed", [CompletedResponse("[tool_code]\nimport google_search\n"), GenerationFailure(ErrorCode.TIMEOUT)]
)
async def test_completed_error_or_status_without_model_content_does_not_record_visibility(completed):
    observation = request_metrics.RequestObservation()
    delivery = LocalDelivery(RecordingTransport())
    with observation.bind():
        await delivery.deliver(
            TelegramTarget(), completed, presentation=FixedPresentation(display_prefix="Synthetic status")
        )
    assert observation.visible is False


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_finishes_first", [False, True])
@pytest.mark.parametrize("submitter", ["manager", "persistent"])
async def test_detached_tts_excludes_parent_metrics_independently_of_scheduling(
    monkeypatch, parent_finishes_first, submitter
):
    from app import metrics
    from app.observability import workload_events
    from app.observability.context import current_context, request_scope
    from app.utils.background_tasks import TaskManager, start_background_task

    @asynccontextmanager
    async def settings_scope():
        yield

    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", settings_scope)
    monkeypatch.setattr(metrics, "metrics_collector", metrics.MetricsCollector())
    operational = OperationalMetrics()
    monkeypatch.setattr(request_metrics, "emit", lambda name, **fields: operational.observe(name, fields))
    monkeypatch.setattr(workload_events, "emit", lambda name, **fields: operational.observe(name, fields))
    parent = request_metrics.RequestObservation()
    gate = asyncio.Event()
    child_scopes = []

    async def tts():
        await gate.wait()
        child_scopes.append((request_metrics.current_request(), current_context().trace_id))
        attempt = workload_events.start_workload_attempt(
            workload="tts", provider="synthetic", model=None, api_key=None, origin="test"
        )
        attempt.record_dispatch()
        attempt.finish(outcome="succeeded")

    manager = TaskManager()
    with request_scope(operation="synthetic.chat"):
        trace_id = current_context().trace_id
        with parent.bind():
            parent.generation_started()
            task = manager.try_submit(tts()) if submitter == "manager" else start_background_task(None, tts, "test-tts")
            assert task is not None
            if parent_finishes_first:
                parent.finish()
            gate.set()
            await task
            parent.finish()
    assert child_scopes == [(None, trace_id)]
    amplification = operational.snapshot()["amplification"]
    assert amplification["accepted_generation_requests"] == 1
    assert amplification["api_attempts"] == 0


@pytest.mark.asyncio
async def test_awaited_request_child_keeps_its_parent_observation():
    observation = request_metrics.RequestObservation()

    async def child():
        return request_metrics.current_request()

    with observation.bind():
        assert await asyncio.create_task(child()) is observation


@pytest.mark.asyncio
async def test_ptb_transport_wait_does_not_enter_logical_other_stage_histogram(monkeypatch):
    from app.observability.ingress import current_update_admitted
    from app.update_processor import UserScopedUpdateProcessor

    @asynccontextmanager
    async def settings_scope():
        yield

    monkeypatch.setattr("app.runtime_settings.lifecycle.runtime_settings_scope", settings_scope)
    monkeypatch.setattr("app.update_processor.time.monotonic", lambda: 3.0)
    operational = OperationalMetrics(clock=lambda: 3.0)
    monkeypatch.setattr("app.update_processor.emit", lambda name, **fields: operational.observe(name, fields))
    monkeypatch.setattr(request_metrics, "emit", lambda name, **fields: operational.observe(name, fields))
    token = current_update_admitted.set(1.0)

    async def handler():
        observation = request_metrics.RequestObservation(workload="multimodal_generation", clock=lambda: 3.0)
        observation.stage("ingress_wait", 1.0)
        observation.finish()

    try:
        await UserScopedUpdateProcessor(1).do_process_update(SimpleNamespace(), handler())
    finally:
        current_update_admitted.reset(token)
    ingress = [row for row in operational.snapshot()["stages"] if row["stage"] == "ingress_wait"]
    assert len(ingress) == 1
    assert ingress[0]["workload"] == "other"
    assert (ingress[0]["count"], ingress[0]["sum_seconds"]) == (1, 1.0)
    assert operational.snapshot()["durations"]["telegram_ingress_wait_ms"] == (1, 2000.0)


def test_completed_generation_wait_rate_names_its_limited_coverage():
    clock = [0.0]
    operational = OperationalMetrics(clock=lambda: clock[0])
    clock[0] = 20.0
    operational.observe(
        "request.stage_finished", {"stage": "generation", "workload": "interactive", "duration_ms": 6000}
    )
    snapshot = operational.snapshot()
    assert "average_active_generations" not in snapshot
    rate = snapshot["completed_generation_wait_seconds_per_window_second"]
    assert rate["value"] == 0.3
    assert rate["coverage"] == "completed_owned_generation_wait_spans"
    assert rate["window_seconds"] == 20
