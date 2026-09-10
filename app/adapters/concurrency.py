import asyncio
import contextvars
import logging
import time
import uuid

from app.config import settings
from app.observability.events import emit

_semaphore_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("semaphore_token", default=None)
_semaphore_acquired_ns: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "semaphore_acquired_ns", default=None
)


class GlobalLLMSemaphore:
    """
    Abstracted global rate limiter / semaphore.
    Backed by Redis ZSET for distributed tracking with graceful degradation to asyncio.Semaphore.
    """

    def __init__(self, limit: int, timeout: int = 120, redis_key: str = "llm_req_semaphore"):
        self._limit = limit
        self._timeout = timeout
        self._local_semaphore = asyncio.Semaphore(limit)
        self._key = redis_key
        self._waiting_count = 0

    async def __aenter__(self):
        wait_started_ns = time.monotonic_ns()
        # Fail fast: bounded wait queue. Max waiters = 3x concurrency limit before rejecting.
        # This prevents 1-hour delays and hanging placeholders when system is swamped.
        waiter_limit = self._limit * 3
        if self._waiting_count >= waiter_limit:
            emit(
                "concurrency.acquire_finished",
                level="warning",
                operation="concurrency.acquire",
                outcome="rejected",
                reason_code="waiter_capacity",
                semaphore=self._key,
                waiting_count=self._waiting_count,
                waiter_limit=waiter_limit,
            )
            from app.errors import UserLimitExceededError

            raise UserLimitExceededError(
                "Система перегружена большим количеством запросов. Пожалуйста, попробуйте еще раз через несколько минут."
            )

        self._waiting_count += 1
        try:
            # 1. Acquire local semaphore first (limits per-node concurrency to max limit)
            await asyncio.wait_for(self._local_semaphore.__aenter__(), timeout=float(self._timeout))
        except TimeoutError:
            from app.errors import UserLimitExceededError

            emit(
                "concurrency.acquire_finished",
                level="warning",
                operation="concurrency.acquire",
                outcome="rejected",
                reason_code="local_timeout",
                semaphore=self._key,
                wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
            )
            raise UserLimitExceededError(
                "Слишком много одновременных запросов. Попробуйте еще раз через несколько секунд."
            ) from None
        finally:
            self._waiting_count -= 1

        token = str(uuid.uuid4())
        _semaphore_token.set(token)
        _semaphore_acquired_ns.set(time.monotonic_ns())

        try:
            from app.cache import redis_client

            if not redis_client:
                emit(
                    "concurrency.acquire_finished",
                    operation="concurrency.acquire",
                    outcome="acquired",
                    mode="local_fallback",
                    reason_code="redis_unavailable",
                    semaphore=self._key,
                    wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
                )
                return self

            started_waiting = time.monotonic()
            while True:
                now = time.time()
                # Clean up zombies
                await redis_client.zremrangebyscore(self._key, 0, now - self._timeout)

                # Check global capacity
                count = await redis_client.zcard(self._key)
                if count < self._limit:
                    await redis_client.zadd(self._key, {token: now})

                    # Verify our rank to avoid race conditions
                    rank = await redis_client.zrank(self._key, token)
                    if rank is not None and rank < self._limit:
                        emit(
                            "concurrency.acquire_finished",
                            operation="concurrency.acquire",
                            outcome="acquired",
                            mode="distributed",
                            semaphore=self._key,
                            wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
                        )
                        return self

                    # Lost the race, remove and wait
                    await redis_client.zrem(self._key, token)

                if (time.monotonic() - started_waiting) > float(self._timeout):
                    from app.errors import UserLimitExceededError

                    raise UserLimitExceededError("Система перегружена. Пожалуйста, повторите запрос немного позже.")
                await asyncio.sleep(0.5)
        except Exception as e:
            emit(
                "concurrency.acquire_finished",
                level="warning",
                operation="concurrency.acquire",
                outcome="acquired",
                mode="local_fallback",
                reason_code="redis_error",
                semaphore=self._key,
                error_type=type(e).__name__,
                wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
            )
            return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            from app.cache import redis_client

            token = _semaphore_token.get()
            if redis_client and token:
                await redis_client.zrem(self._key, token)
        except Exception as e:
            logging.warning("Error releasing Redis distributed semaphore: %s", e)
        finally:
            await self._local_semaphore.__aexit__(exc_type, exc_val, exc_tb)
            acquired_ns = _semaphore_acquired_ns.get()
            emit(
                "concurrency.released",
                level="warning" if exc_type is not None else "debug",
                operation="concurrency.release",
                semaphore=self._key,
                outcome="released",
                hold_ms=(round((time.monotonic_ns() - acquired_ns) / 1_000_000, 2) if acquired_ns else None),
                scope_error_type=exc_type.__name__ if exc_type is not None else None,
            )
            _semaphore_token.set(None)
            _semaphore_acquired_ns.set(None)


class _LazyGlobalLLMSemaphore:
    """Proxy that defers semaphore creation until first use.

    This prevents import-time access to ``settings.*``, which would fail in
    test workers that have ``app.config`` mocked as a ``MagicMock``.
    """

    def __init__(self, settings_attr: str, redis_key: str) -> None:
        self._settings_attr = settings_attr
        self._redis_key = redis_key
        self._inner: GlobalLLMSemaphore | None = None

    def _ensure(self) -> GlobalLLMSemaphore:
        if self._inner is None:
            limit = max(1, int(getattr(settings, self._settings_attr)))
            self._inner = GlobalLLMSemaphore(limit, redis_key=self._redis_key)
        return self._inner

    async def __aenter__(self) -> GlobalLLMSemaphore:
        return await self._ensure().__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._inner is not None:
            await self._inner.__aexit__(exc_type, exc_val, exc_tb)


heavy_request_semaphore = _LazyGlobalLLMSemaphore("MAX_CONCURRENT_HEAVY_REQUESTS", redis_key="llm_req_semaphore")
ultra_heavy_semaphore = _LazyGlobalLLMSemaphore(
    "MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS", redis_key="llm_ultra_heavy_semaphore"
)
