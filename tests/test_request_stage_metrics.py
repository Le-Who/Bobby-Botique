"""Request metrics distinguish logical work from transport dispatch."""

import asyncio
import math
from unittest.mock import AsyncMock

import pytest

from app.observability.metrics import OperationalMetrics


def test_request_count_has_one_logical_owner():
    metrics = OperationalMetrics()
    for event in ("http.request_finished", "telegram.update_finished", "request.finished"):
        metrics.observe(event, {"outcome": "succeeded", "duration_ms": 900})
    assert metrics.snapshot()["request_outcomes"] == {"succeeded": 1}
    assert metrics.snapshot()["transport_outcomes"] == {
        ("http", "succeeded"): 1,
        ("telegram", "succeeded"): 1,
    }


def test_histogram_window_rolls_over_but_prometheus_is_cumulative():
    clock = [120.0]
    metrics = OperationalMetrics(clock=lambda: clock[0])
    for _ in range(20):
        metrics.observe("request.stage_finished", {"stage": "context", "workload": "interactive", "duration_ms": 75})
    row = metrics.snapshot()["stages"][0]
    assert row["count"] == 20
    assert row["sum_seconds"] == pytest.approx(1.5)
    assert 0.05 <= row["p95_seconds"] <= 0.1
    assert row["low_sample"] is False
    assert row["scope"] == "process"
    clock[0] += 900
    assert metrics.snapshot()["stages"][0]["count"] == 0
    exported = metrics.prometheus_text()
    assert 'stage="context",workload="interactive",le="0.1"} 20' in exported
    assert 'stage="context",workload="interactive"} 20' in exported


def test_stage_labels_and_invalid_values_are_bounded():
    metrics = OperationalMetrics(max_provider_pairs=2)
    for value in (-1, math.nan, math.inf, True):
        metrics.observe("request.stage_finished", {"stage": "total", "workload": "interactive", "duration_ms": value})
    for index in range(100):
        metrics.observe(
            "request.stage_finished", {"stage": f"secret-{index}", "workload": f"prompt-{index}", "duration_ms": 1}
        )
    assert metrics.snapshot()["stages"] == []
    for stage in ("context", "delivery", "total", "generation"):
        metrics.observe("request.stage_finished", {"stage": stage, "workload": "private body", "duration_ms": 10})
    assert len(metrics.snapshot()["stages"]) <= 3
    assert "private body" not in metrics.prometheus_text()


def test_amplification_counts_generation_admission_only_and_rolls_over():
    clock = [60.0]
    metrics = OperationalMetrics(clock=lambda: clock[0])
    metrics.observe("request.accepted", {"workload": "interactive", "generation": False})
    metrics.observe("request.accepted", {"workload": "interactive", "generation": True})
    for _ in range(3):
        metrics.observe(
            "provider.attempt_started", {"provider": "gemini", "actual_model": "test", "request_generation": True}
        )
    metrics.observe("provider.attempt_started", {"provider": "gemini", "actual_model": "unowned"})
    result = metrics.snapshot()["amplification"]
    assert result["accepted_generation_requests"] == 1
    assert result["api_attempts"] == 3
    assert result["ratio"] == 3
    clock[0] += 900
    assert metrics.snapshot()["amplification"]["ratio"] is None


def test_owned_stages_use_acceptance_clock_and_one_terminal(monkeypatch):
    from app.observability import request_metrics

    clock = [10.0]
    events = []
    monkeypatch.setattr(request_metrics, "emit", lambda event, **fields: events.append((event, fields)))
    observation = request_metrics.RequestObservation(clock=lambda: clock[0])
    clock[0] = 12
    with observation.bind():
        observation.stage("ingress_wait", 2)
        clock[0] = 15
        observation.stage("context", 3)
        observation.generation_started()
        observation.generation_started()
        clock[0] = 19
        request_metrics.record_first_visible_text()
        request_metrics.record_first_visible_text()
        observation.set_outcome("partial")
        clock[0] = 20
        observation.finish()
        observation.finish()
    stages = {fields["stage"]: fields["duration_ms"] for name, fields in events if name == "request.stage_finished"}
    assert stages == {"ingress_wait": 2000, "context": 3000, "first_visible_text": 9000, "total": 10000}
    terminals = [fields for name, fields in events if name == "request.finished"]
    assert len(terminals) == 1
    assert terminals[0]["outcome"] == "partial"
    assert sum(name == "request.accepted" for name, _ in events) == 1
    assert request_metrics.current_request() is None


@pytest.mark.asyncio
async def test_visible_text_waits_for_successful_telegram_write(monkeypatch):
    from app.observability.request_metrics import RequestObservation
    from app.response_delivery.renderer import TelegramMessageRef, TelegramRenderer

    class Transport:
        current_ref = TelegramMessageRef(chat_id=1, message_id=2)
        edit = AsyncMock(side_effect=[RuntimeError("synthetic"), None])

    observation = RequestObservation()
    session = TelegramRenderer(Transport(), min_chunk_size=1, debounce_seconds=0).open()
    with observation.bind():
        await session.append("answer")
        assert observation.visible is False
        await session.append(" more")
        assert observation.visible is True
    assert Transport.edit.await_count == 2


@pytest.mark.asyncio
async def test_task_manager_gauges_cover_accepted_rejected_running_and_completion():
    from app.utils.background_tasks import TaskManager

    manager = TaskManager()
    manager.MAX_TASKS = 1
    gate = asyncio.Event()
    task = manager.try_submit(gate.wait())
    assert task is not None
    assert manager.try_submit(gate.wait()) is None
    await asyncio.sleep(0)
    running = manager.metrics_snapshot()
    assert (running["outstanding"], running["running"], running["accepted_total"], running["rejected_total"]) == (
        1,
        1,
        1,
        1,
    )
    gate.set()
    await task
    await asyncio.sleep(0)
    assert manager.metrics_snapshot()["running"] == 0
    assert manager.metrics_snapshot()["outstanding"] == 0


@pytest.mark.asyncio
async def test_provider_and_delivery_time_are_not_counted_twice(monkeypatch):
    from app.observability import request_metrics

    clock = [0.0]
    metrics = OperationalMetrics(clock=lambda: clock[0])
    monkeypatch.setattr(request_metrics, "emit", lambda name, **fields: metrics.observe(name, fields))
    observation = request_metrics.RequestObservation(clock=lambda: clock[0])

    @request_metrics.observe_generation
    async def generate():
        clock[0] += 4
        yield "synthetic answer"

    @request_metrics.observe_transport
    async def send(_text):
        clock[0] += 2
        request_metrics.record_first_visible_text()

    with observation.bind():
        clock[0] += 1
        observation.stage("ingress_wait", 1)
        clock[0] += 2
        observation.stage("admission_wait", 2)
        clock[0] += 3
        observation.stage("context", 3)
        async for text in generate():
            await send(text)
        observation.finish()
    stages = {row["stage"]: row["sum_seconds"] for row in metrics.snapshot()["stages"]}
    assert stages == {
        "ingress_wait": 1,
        "admission_wait": 2,
        "context": 3,
        "generation": 4,
        "delivery": 2,
        "first_visible_text": 12,
        "total": 12,
    }
