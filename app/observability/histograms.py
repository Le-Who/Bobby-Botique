"""Fixed-memory histograms and minute windows; callers own synchronization."""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 900, math.inf)
STAGES = frozenset(
    {"ingress_wait", "admission_wait", "context", "first_visible_text", "generation", "delivery", "total"}
)
WORKLOADS = frozenset(
    {
        "interactive",
        "quick_search",
        "deferred_retry",
        "inline",
        "background",
        "deferred",
        "research",
        "image_generation",
        "tts",
        "stt",
        "audio_generation",
        "daily_game_generation",
        "document_processing",
        "maintenance",
        "other",
    }
)
WINDOW_MINUTES = 15


def workload_label(value: object) -> str:
    return value if isinstance(value, str) and value in WORKLOADS else "other"


@dataclass
class Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * len(BUCKETS))
    total: float = 0.0

    def observe(self, seconds: float) -> None:
        self.counts[bisect_left(BUCKETS, seconds)] += 1
        self.total += seconds

    @property
    def count(self) -> int:
        return sum(self.counts)

    def merge(self, other: Histogram) -> None:
        self.total += other.total
        for index, count in enumerate(other.counts):
            self.counts[index] += count

    def quantile(self, fraction: float) -> float | None:
        count = self.count
        if not count:
            return None
        target = count * fraction
        previous = 0.0
        consumed = 0
        for upper, size in zip(BUCKETS, self.counts, strict=True):
            if size and consumed + size >= target:
                # Values above the last finite bucket are censored, never JSON Infinity.
                return previous if math.isinf(upper) else previous + (upper - previous) * (target - consumed) / size
            consumed += size
            previous = upper
        return None


class WindowHistogram:
    def __init__(self) -> None:
        self.cumulative = Histogram()
        self.segments: dict[int, Histogram] = {}

    def _prune(self, minute: int) -> None:
        for old in tuple(self.segments):
            if old <= minute - WINDOW_MINUTES or old > minute:
                del self.segments[old]

    def observe(self, seconds: float, now: float) -> None:
        minute = int(now // 60)
        self._prune(minute)
        self.segments.setdefault(minute, Histogram()).observe(seconds)
        self.cumulative.observe(seconds)

    def window(self, now: float) -> Histogram:
        self._prune(int(now // 60))
        result = Histogram()
        for segment in self.segments.values():
            result.merge(segment)
        return result


class WindowCounter:
    def __init__(self) -> None:
        self.segments: dict[int, int] = {}

    def total(self, now: float) -> int:
        minute = int(now // 60)
        self.segments = {key: value for key, value in self.segments.items() if minute - WINDOW_MINUTES < key <= minute}
        return sum(self.segments.values())

    def increment(self, now: float) -> None:
        self.total(now)
        minute = int(now // 60)
        self.segments[minute] = self.segments.get(minute, 0) + 1
