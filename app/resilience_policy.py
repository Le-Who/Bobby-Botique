import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.circuit_breaker import get_circuit_breaker


@dataclass
class ResiliencePolicy:
    max_retries: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 10.0
    timeout_s: float = 120.0
    jitter_s: float = 0.25

    def compute_delay(self, attempt: int) -> float:
        delay = min(self.base_delay_s * (2**attempt), self.max_delay_s)
        return delay + random.uniform(0.0, self.jitter_s)


def is_transient_error(error_text: str) -> bool:
    lowered = error_text.lower()
    patterns = [
        "503",
        "unavailable",
        "overloaded",
        "rate limit",
        "timeout",
        "connection",
        "temporarily",
    ]
    return any(token in lowered for token in patterns)


# Exception types that are always retryable regardless of their str() representation.
# asyncio.TimeoutError is critical: str(asyncio.TimeoutError()) == "" (empty string),
# so is_transient_error(str(e)) silently returns False, bypassing all retries.
_RETRYABLE_TYPES = (
    TimeoutError,
    ConnectionError,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def is_retryable_exception(exc: Exception) -> bool:
    """Type-aware retryability check: known transient types + text scan."""
    # Status is authoritative: transient words in a 400/401 body must not
    # cause extra requests, while an empty 502 message must still be retried.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_HTTP_STATUSES
    if isinstance(exc, _RETRYABLE_TYPES):
        return True
    return is_transient_error(str(exc))


async def run_with_resilience[T](
    operation: Callable[[], Awaitable[T]],
    policy: ResiliencePolicy | None = None,
    *,
    circuit_name: str | None = None,
    is_retryable: Callable[[Exception], bool] = is_retryable_exception,
) -> tuple[T, int]:
    effective = policy or ResiliencePolicy()
    last_error: Exception | None = None

    async def _call_once() -> T:
        return await asyncio.wait_for(operation(), timeout=effective.timeout_s)

    for attempt in range(effective.max_retries):
        try:
            if circuit_name:
                breaker = get_circuit_breaker(circuit_name)
                result = await breaker.call(_call_once)
            else:
                result = await _call_once()
            return result, attempt + 1
        except Exception as exc:
            last_error = exc
            if attempt >= effective.max_retries - 1 or not is_retryable(exc):
                raise
            await asyncio.sleep(effective.compute_delay(attempt))

    if last_error:
        raise last_error
    raise RuntimeError("Resilience runner failed without explicit error")
