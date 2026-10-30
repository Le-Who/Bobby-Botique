"""Pure queue timing policy; Redis adapters apply durations to server time."""

import math


def next_attempt_at(
    attempts_started: int,
    now: float,
    retry_after: float | None = None,
    random_value: float = 0,
) -> float:
    if not math.isfinite(now) or not 0 <= random_value <= 1:
        raise ValueError("Invalid queue clock or jitter")
    base = min(60.0, 15.0 * 2 ** min(2, max(0, attempts_started - 1)))
    delay = base * (1 + 0.2 * random_value)
    if retry_after is not None and math.isfinite(retry_after):
        delay = max(delay, retry_after)
    return now + delay
