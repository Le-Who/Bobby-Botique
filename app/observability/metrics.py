"""Bounded process-local operational metrics derived from owned events."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from app.observability.histograms import BUCKETS, STAGES, WindowCounter, WindowHistogram, workload_label

_LABEL_CHARACTERS = re.compile(r"[^a-zA-Z0-9_.:/\\-]+")
_OUTCOMES = {
    "cancelled",
    "deferred",
    "failed",
    "failure_notice_sent",
    "invalid_response",
    "partial",
    "remote_error",
    "sent",
    "succeeded",
    "transport_failed",
    "unknown",
    "rejected",
    "expired",
    "unknown_delivery",
}


def _label(value: object, *, fallback: str = "unknown") -> str:
    if not isinstance(value, str) or not value:
        return fallback
    return _LABEL_CHARACTERS.sub("_", value)[:96] or fallback


def _outcome(value: object) -> str:
    normalized = _label(value)
    return normalized if normalized in _OUTCOMES else "other"


def _job_outcome(fields: Mapping[str, Any]) -> str:
    explicit = fields.get("outcome")
    if explicit is not None:
        return _outcome(explicit)
    execution_outcome = fields.get("execution_outcome")
    business_outcome = fields.get("business_outcome")
    if execution_outcome == "raised" or business_outcome == "failed":
        return "failed"
    if execution_outcome == "returned":
        return "succeeded"
    return "unknown"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _prometheus_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class OperationalMetrics:
    """Small, thread-safe counters that never retain request or actor identifiers."""

    def __init__(self, *, max_provider_pairs: int = 256, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._max_provider_pairs = max(1, max_provider_pairs)
        self._provider_pairs: set[tuple[str, str]] = set()
        self._provider_attempts: Counter[tuple[str, str, str]] = Counter()
        self._workload_dimensions: set[tuple[str, str, str]] = set()
        self._workload_attempts: Counter[tuple[str, str, str, str]] = Counter()
        self._request_outcomes: Counter[str] = Counter()
        self._transport_outcomes: Counter[tuple[str, str]] = Counter()
        self._stages: dict[tuple[str, str], WindowHistogram] = {}
        self._generation_admissions = WindowCounter()
        self._api_attempts = WindowCounter()
        self._race_windows: dict[tuple[str, str, str, str], WindowCounter] = {}
        self._delivery_outcomes: Counter[str] = Counter()
        self._job_outcomes: Counter[str] = Counter()
        self._logging_losses: Counter[tuple[str, str]] = Counter()
        self._durations: dict[str, list[float]] = {
            "provider_duration_ms": [0.0, 0.0],
            "provider_ttft_ms": [0.0, 0.0],
            "workload_duration_ms": [0.0, 0.0],
            "request_duration_ms": [0.0, 0.0],
            "delivery_duration_ms": [0.0, 0.0],
            "queue_wait_ms": [0.0, 0.0],
            "pool_wait_ms": [0.0, 0.0],
            "telegram_ingress_wait_ms": [0.0, 0.0],
        }

    def observe(self, event: str, fields: Mapping[str, Any]) -> None:
        """Record one stable owner-emitted event without retaining its payload."""
        with self._lock:
            if event == "request.stage_finished":
                self._observe_stage(fields)
            elif event == "telegram.ingress_wait_finished":
                self._add_duration("telegram_ingress_wait_ms", fields.get("duration_ms"))
            elif event == "request.accepted" and fields.get("generation") is True:
                self._generation_admissions.increment(self._clock())
            elif event == "request.api_attempt" or (
                event == "provider.attempt_started" and fields.get("request_generation") is True
            ):
                self._api_attempts.increment(self._clock())
            elif event == "provider.race_outcome":
                self._observe_race(fields)
            elif event == "provider.attempt_finished":
                provider = _label(fields.get("provider"))
                model = _label(fields.get("actual_model"))
                pair = (provider, model)
                if pair not in self._provider_pairs:
                    if len(self._provider_pairs) >= self._max_provider_pairs:
                        pair = ("other", "other")
                    else:
                        self._provider_pairs.add(pair)
                self._provider_attempts[(*pair, _outcome(fields.get("outcome")))] += 1
                self._add_duration("provider_duration_ms", fields.get("duration_ms"))
                if fields.get("outcome") == "cancelled" and fields.get("text_emitted") is False:
                    self._observe_race({**fields, "race_outcome": "cancelled_before_text"})
            elif event == "workload.attempt_finished":
                dimensions = (
                    _label(fields.get("workload")),
                    _label(fields.get("provider")),
                    _label(fields.get("model")),
                )
                if dimensions not in self._workload_dimensions:
                    if len(self._workload_dimensions) >= self._max_provider_pairs:
                        dimensions = ("other", "other", "other")
                    else:
                        self._workload_dimensions.add(dimensions)
                self._workload_attempts[(*dimensions, _outcome(fields.get("outcome")))] += 1
                self._add_duration("workload_duration_ms", fields.get("duration_ms"))
            elif event == "provider.first_text":
                self._add_duration("provider_ttft_ms", fields.get("ttft_ms"))
            elif event in {"telegram.update_finished", "http.request_finished"}:
                self._transport_outcomes[(event.split(".")[0], _outcome(fields.get("outcome")))] += 1
            elif event == "request.finished":
                self._request_outcomes[_outcome(fields.get("outcome"))] += 1
                self._add_duration("request_duration_ms", fields.get("duration_ms"))
            elif event == "delivery.finished":
                delivery_outcome = fields.get("outcome") or fields.get("delivery_status")
                self._delivery_outcomes[_outcome(delivery_outcome)] += 1
                self._add_duration("delivery_duration_ms", fields.get("duration_ms"))
            elif event == "job.started":
                self._add_duration("queue_wait_ms", fields.get("queue_wait_ms"))
            elif event == "job.finished":
                self._job_outcomes[_job_outcome(fields)] += 1
            elif event == "database.operation_finished":
                self._add_duration("pool_wait_ms", fields.get("pool_wait_ms"))
            elif event == "logging.loss_summary":
                level = _label(fields.get("dropped_level"))
                reason = _label(fields.get("reason_code"))
                count = fields.get("dropped_count", 1)
                self._logging_losses[(level, reason)] += int(count) if isinstance(count, int) else 1

    def _observe_stage(self, fields: Mapping[str, Any]) -> None:
        if fields.get("scope") not in {None, "logical_request"}:
            return
        stage = fields.get("stage")
        duration = _number(fields.get("duration_ms"))
        if not isinstance(stage, str) or stage not in STAGES or duration is None:
            return
        pair = (stage, workload_label(fields.get("workload")))
        if pair not in self._stages and len(self._stages) >= self._max_provider_pairs:
            pair = ("other", "other")
        self._stages.setdefault(pair, WindowHistogram()).observe(duration / 1000, self._clock())

    def _observe_race(self, fields: Mapping[str, Any]) -> None:
        outcome = fields.get("race_outcome")
        if outcome not in {"winner", "late_loser", "cancelled_before_text"}:
            return
        kind = str(fields.get("race_kind", "attempt"))
        if kind not in {"model", "key", "attempt"}:
            kind = "other"
        pair = (_label(fields.get("actual_model")), workload_label(fields.get("workload")), str(outcome), kind)
        if pair not in self._race_windows and len(self._race_windows) >= self._max_provider_pairs:
            pair = ("other", "other", "other", "other")
        self._race_windows.setdefault(pair, WindowCounter()).increment(self._clock())

    def _stage_snapshot(self, now: float) -> list[dict[str, Any]]:
        rows = []
        for (stage, workload), value in sorted(self._stages.items()):
            window = value.window(now)
            rows.append(
                {
                    "stage": stage,
                    "workload": workload,
                    "count": window.count,
                    "sum_seconds": window.total,
                    "p50_seconds": window.quantile(0.50),
                    "p95_seconds": window.quantile(0.95),
                    "p99_seconds": window.quantile(0.99),
                    "low_sample": window.count < 20,
                    "approximate": True,
                    "window_seconds": 900,
                    "segment_seconds": 60,
                    "scope": "process",
                    "observed_at": time.time(),
                    "available": True,
                    "overflow_count": window.counts[-1],
                    "cumulative_counts": tuple(value.cumulative.counts),
                    "cumulative_sum_seconds": value.cumulative.total,
                }
            )
        return rows

    def _add_duration(self, name: str, value: object) -> None:
        number = _number(value)
        if number is not None:
            aggregate = self._durations[name]
            aggregate[0] += 1
            aggregate[1] += number

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            accepted = self._generation_admissions.total(now)
            attempts = self._api_attempts.total(now)
            generation_seconds = sum(
                value.window(now).total for (stage, _), value in self._stages.items() if stage == "generation"
            )
            observed_window = min(900.0, max(0, now - self._started))
            return {
                "uptime_seconds": max(0.0, now - self._started),
                "provider_attempts": dict(self._provider_attempts),
                "workload_attempts": dict(self._workload_attempts),
                "request_outcomes": dict(self._request_outcomes),
                "transport_outcomes": dict(self._transport_outcomes),
                "stages": self._stage_snapshot(now),
                "races": [
                    {
                        "model": model,
                        "workload": workload,
                        "outcome": outcome,
                        "kind": kind,
                        "count": counter.total(now),
                        "scope": "process",
                        "window_seconds": 900,
                    }
                    for (model, workload, outcome, kind), counter in sorted(self._race_windows.items())
                ],
                "amplification": {
                    "accepted_generation_requests": accepted,
                    "api_attempts": attempts,
                    "ratio": attempts / accepted if accepted else None,
                    "window_seconds": 900,
                    "scope": "process",
                    "available": True,
                    "observed_at": time.time(),
                    "coverage": "owned_routed_and_direct_generation_scopes",
                },
                "completed_generation_wait_seconds_per_window_second": {
                    "value": generation_seconds / observed_window if observed_window else None,
                    "scope": "process",
                    "window_seconds": observed_window,
                    "coverage": "completed_owned_generation_wait_spans",
                    "approximate": True,
                },
                "delivery_outcomes": dict(self._delivery_outcomes),
                "job_outcomes": dict(self._job_outcomes),
                "logging_losses": dict(self._logging_losses),
                "durations": {name: tuple(values) for name, values in self._durations.items()},
            }

    def prometheus_text(self) -> str:
        snapshot = self.snapshot()
        lines = [
            "# HELP gembot_uptime_seconds Time since process start.",
            "# TYPE gembot_uptime_seconds gauge",
            f"gembot_uptime_seconds {snapshot['uptime_seconds']:.3f}",
            "# HELP gembot_provider_attempts_total Provider attempts by actual provider/model/outcome.",
            "# TYPE gembot_provider_attempts_total counter",
        ]
        for (provider, model, outcome), count in sorted(snapshot["provider_attempts"].items()):
            lines.append(
                "gembot_provider_attempts_total"
                f'{{provider="{_prometheus_escape(provider)}",model="{_prometheus_escape(model)}",'
                f'outcome="{_prometheus_escape(outcome)}"}} {count}'
            )
        lines.extend(
            [
                "# HELP gembot_workload_attempts_total Direct workload attempts by workload/provider/model/outcome.",
                "# TYPE gembot_workload_attempts_total counter",
            ]
        )
        for (workload, provider, model, outcome), count in sorted(snapshot["workload_attempts"].items()):
            lines.append(
                "gembot_workload_attempts_total"
                f'{{workload="{_prometheus_escape(workload)}",provider="{_prometheus_escape(provider)}",'
                f'model="{_prometheus_escape(model)}",outcome="{_prometheus_escape(outcome)}"}} {count}'
            )
        for metric, values in (
            ("request", snapshot["request_outcomes"]),
            ("delivery", snapshot["delivery_outcomes"]),
            ("job", snapshot["job_outcomes"]),
        ):
            lines.extend(
                [
                    f"# HELP gembot_{metric}_outcomes_total Terminal {metric} outcomes.",
                    f"# TYPE gembot_{metric}_outcomes_total counter",
                ]
            )
            for outcome, count in sorted(values.items()):
                lines.append(f'gembot_{metric}_outcomes_total{{outcome="{_prometheus_escape(outcome)}"}} {count}')
        lines.extend(
            [
                "# HELP gembot_logging_dropped_events_total Logging events dropped by level and reason.",
                "# TYPE gembot_logging_dropped_events_total counter",
            ]
        )
        for (level, reason), count in sorted(snapshot["logging_losses"].items()):
            lines.append(
                "gembot_logging_dropped_events_total"
                f'{{level="{_prometheus_escape(level)}",reason="{_prometheus_escape(reason)}"}} {count}'
            )
        for name, (count, total) in sorted(snapshot["durations"].items()):
            lines.append(f"# TYPE gembot_{name} summary")
            lines.append(f"gembot_{name}_count {int(count)}")
            lines.append(f"gembot_{name}_sum {total:.3f}")
        lines.append("# TYPE gembot_request_stage_seconds histogram")
        for row in snapshot["stages"]:
            labels = f'stage="{row["stage"]}",workload="{row["workload"]}"'
            cumulative_count = 0
            for upper, count in zip(BUCKETS, row["cumulative_counts"], strict=True):
                cumulative_count += count
                bound = "+Inf" if math.isinf(upper) else str(upper)
                lines.append(f'gembot_request_stage_seconds_bucket{{{labels},le="{bound}"}} {cumulative_count}')
            lines.append(f"gembot_request_stage_seconds_count{{{labels}}} {cumulative_count}")
            lines.append(f"gembot_request_stage_seconds_sum{{{labels}}} {row['cumulative_sum_seconds']:.6f}")
        lines.append("# TYPE gembot_transport_outcomes_total counter")
        for (transport, outcome), count in sorted(snapshot["transport_outcomes"].items()):
            lines.append(f'gembot_transport_outcomes_total{{transport="{transport}",outcome="{outcome}"}} {count}')
        return "\n".join(lines) + "\n"


operational_metrics = OperationalMetrics()


__all__ = ["OperationalMetrics", "operational_metrics"]
