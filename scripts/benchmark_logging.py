#!/usr/bin/env python3
"""Synthetic bounded-writer benchmark; never reads application data or secrets."""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.observability.pipeline import standalone_event_bytes
from app.observability.writer import BoundedLogWriter


class _Sink:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.lines = 0
        self.bytes = 0

    def write(self, value: str) -> int:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        self.lines += 1
        self.bytes += len(value.encode())
        return len(value)

    def flush(self) -> None:
        return None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return ordered[index]


def run(events: int, sink_delay_ms: float) -> dict[str, object]:
    sink = _Sink(max(0.0, sink_delay_ms) / 1000)
    writer = BoundedLogWriter(sink, max_events=4_096, max_bytes=8_388_608)
    latencies_ms: list[float] = []
    process_started = time.process_time()
    wall_started = time.perf_counter()
    for index in range(events):
        started_ns = time.perf_counter_ns()
        payload = standalone_event_bytes(
            "benchmark.synthetic",
            level="info",
            message="Synthetic logging benchmark",
            fields={
                "sequence": index,
                "provider": "synthetic",
                "key_suffix": "1234",
                "input_chars": 256,
            },
        )
        writer.submit(payload, levelno=logging.INFO)
        latencies_ms.append((time.perf_counter_ns() - started_ns) / 1_000_000)
    drained = writer.stop(timeout=3.0)
    elapsed = time.perf_counter() - wall_started
    cpu_seconds = time.process_time() - process_started
    try:
        import psutil

        rss_bytes = psutil.Process(os.getpid()).memory_info().rss
    except Exception:
        rss_bytes = None
    dropped = sum(writer.dropped_by_level.values())
    return {
        "events_submitted": events,
        "events_written": sink.lines,
        "bytes_written": sink.bytes,
        "elapsed_seconds": round(elapsed, 6),
        "throughput_events_per_second": round(events / elapsed, 2) if elapsed else None,
        "producer_latency_ms": {
            "p50": round(statistics.median(latencies_ms), 6),
            "p95": round(_percentile(latencies_ms, 0.95), 6),
            "p99": round(_percentile(latencies_ms, 0.99), 6),
        },
        "cpu_seconds": round(cpu_seconds, 6),
        "rss_bytes": rss_bytes,
        "drops": dropped,
        "drop_reasons": dict(writer.dropped_by_reason),
        "writer_failures": writer.write_failures,
        "drained_within_3s": drained,
        "sink_delay_ms": sink_delay_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=10_000)
    parser.add_argument("--sink-delay-ms", type=float, default=0.0)
    args = parser.parse_args()
    if args.events <= 0:
        parser.error("--events must be positive")
    print(json.dumps(run(args.events, args.sink_delay_ms), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
