"""Owned logical-request timings inherited by request-scoped async children."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar

from app.observability.events import emit
from app.observability.histograms import STAGES, workload_label

_current: ContextVar[RequestObservation | None] = ContextVar("request_observation", default=None)


class RequestObservation:
    """No actor, prompt, receipt or task identifiers are retained in metrics state."""

    def __init__(
        self,
        *,
        workload: str = "interactive",
        clock: Callable[[], float] = time.monotonic,
        started: float | None = None,
    ):
        self.clock = clock
        self.started = clock() if started is None else started
        self.workload = workload_label(workload)
        self.outcome = "succeeded"
        self.generation = False
        self.finished = False
        self.visible = False
        self._stages: dict[str, float] = {}

    @contextmanager
    def bind(self) -> Iterator[RequestObservation]:
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def stage(self, stage: str, seconds: float) -> None:
        if self.finished or stage not in STAGES:
            return
        self._stages[stage] = self._stages.get(stage, 0) + max(0, seconds)

    def generation_started(self) -> None:
        if not self.finished and not self.generation:
            self.generation = True
            emit("request.accepted", workload=self.workload, generation=True)

    def first_visible_text(self) -> None:
        if not self.visible and not self.finished:
            self.visible = True
            self.stage("first_visible_text", self.clock() - self.started)

    def set_outcome(self, outcome: str) -> None:
        if not self.finished and outcome in {"failed", "cancelled", "partial", "rejected", "deferred"}:
            # Binary legacy error accounting is less specific than a terminal
            # already supplied by delivery/admission. Preserve that owner fact.
            if outcome == "failed" and self.outcome != "succeeded":
                return
            self.outcome = outcome

    def finish(self) -> None:
        if self.finished:
            return
        elapsed = self.clock() - self.started
        self.stage("total", elapsed)
        self.finished = True
        for stage, seconds in self._stages.items():
            emit(
                "request.stage_finished",
                stage=stage,
                workload=self.workload,
                duration_ms=seconds * 1000,
                scope="logical_request",
            )
        emit("request.finished", workload=self.workload, outcome=self.outcome, duration_ms=max(0, elapsed) * 1000)


def current_request() -> RequestObservation | None:
    observation = _current.get()
    return observation if observation is not None and not observation.finished else None


def detached_request_context(context: Context) -> Context:
    """Retain tracing/privacy/settings context without a live parent observation.

    Awaited request children inherit normally. A detached job needs its own
    explicit request lifetime to contribute to owned-generation amplification.
    """
    detached = context.copy()
    detached.run(_current.set, None)
    return detached


def record_first_visible_text() -> None:
    if observation := current_request():
        observation.first_visible_text()


def record_stage(stage: str, seconds: float) -> None:
    if observation := current_request():
        observation.stage(stage, seconds)


def record_race_outcome(model: str, outcome: str, *, kind: str) -> None:
    observation = current_request()
    emit(
        "provider.race_outcome",
        actual_model=model,
        race_outcome=outcome,
        race_kind=kind,
        workload=observation.workload if observation else "other",
    )


def observe_transport(function):
    @functools.wraps(function)
    async def measured(*args, **kwargs):
        observation = current_request()
        clock = observation.clock if observation is not None else time.monotonic
        started = clock()
        try:
            return await function(*args, **kwargs)
        finally:
            record_stage("delivery", clock() - started)

    return measured


def observe_generation(function):
    """Time the whole routed stream once, independently of speculative attempts."""

    @functools.wraps(function)
    async def measured(*args, **kwargs):
        observation = current_request()
        if observation is not None:
            request = args[1] if len(args) > 1 else kwargs.get("request")
            workload = getattr(request, "workload", None)
            if isinstance(workload, str) and not observation.generation:
                observation.workload = workload_label(workload)
            observation.generation_started()
        clock = observation.clock if observation is not None else time.monotonic
        elapsed = 0.0
        iterator = function(*args, **kwargs)
        try:
            while True:
                started = clock()
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    break
                finally:
                    elapsed += clock() - started
                yield event
        except asyncio.CancelledError:
            if observation is not None:
                observation.set_outcome("cancelled")
            raise
        finally:
            try:
                await iterator.aclose()
            finally:
                if observation is not None:
                    observation.stage("generation", elapsed)

    return measured
