import asyncio
import contextvars
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.observability.events import emit

_ADMIT_SLOT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - tonumber(ARGV[1]))
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
return 1
"""

_RENEW_SLOT = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    return 0
end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
redis.call('ZADD', KEYS[1], 'XX', now, ARGV[1])
return 1
"""


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


@dataclass
class _SemaphoreScope:
    token: str
    owner: asyncio.Task | None
    redis: Any = None
    last_confirmed: float | None = None
    lease_lost: bool = False
    cancellation_marker: object = field(default_factory=object)
    cancellation_count: int = 0
    cancellation_sent: bool = False
    mode: str = "local_fallback"
    metrics_counted: bool = False


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
        self._admission_waiters = 0
        self._active_modes: dict[str, int] = {"distributed": 0, "local_fallback": 0}
        self._token: contextvars.ContextVar[str | None] = contextvars.ContextVar(f"{redis_key}_token", default=None)
        self._acquired_ns: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            f"{redis_key}_acquired", default=None
        )
        self._renewal: contextvars.ContextVar[asyncio.Task | None] = contextvars.ContextVar(
            f"{redis_key}_renewal", default=None
        )
        self._scope: contextvars.ContextVar[_SemaphoreScope | None] = contextvars.ContextVar(
            f"{redis_key}_scope", default=None
        )
        # Admission and lease lifetime are separate policies even while their
        # initial defaults match. Changing a wait budget must not shorten a lease.
        self._lease_ttl = float(timeout)
        self._renew_interval = max(0.01, self._lease_ttl / 3)
        self._cleanup_timeout = 5.0

    def _lose_lease(self, scope: _SemaphoreScope) -> None:
        scope.lease_lost = True
        if scope.owner is not None and not scope.owner.done() and scope.owner.cancelling() <= scope.cancellation_count:
            scope.cancellation_sent = True
            scope.owner.cancel(scope.cancellation_marker)

    async def _renew_slot(self, redis, token: str) -> None:
        scope = self._scope.get()
        assert scope is not None and scope.last_confirmed is not None
        while True:
            safe_until = scope.last_confirmed + self._lease_ttl
            try:
                await asyncio.sleep(min(self._renew_interval, _remaining(safe_until)))
                # Count from before the command: delayed acknowledgement cannot
                # extend the local validity of the server's earlier renewal.
                started = time.monotonic()
                async with asyncio.timeout(min(5.0, _remaining(safe_until))):
                    renewed = await redis.eval(_RENEW_SLOT, 1, self._key, token)
                _remaining(safe_until)
                if renewed != 1:
                    self._lose_lease(scope)
                    return
                scope.last_confirmed = started
            except Exception as error:
                logging.warning("Semaphore renewal unavailable key=%s type=%s", self._key, type(error).__name__)
                if time.monotonic() >= safe_until:
                    self._lose_lease(scope)
                    return

    def metrics_snapshot(self) -> dict[str, Any]:
        return {
            "scope": "process",
            "available": True,
            "observed_at": time.time(),
            "waiters": self._admission_waiters,
            "active": sum(self._active_modes.values()),
            "capacity": self._limit,
            "modes": dict(self._active_modes),
            "degraded": self._active_modes["local_fallback"] > 0,
        }

    async def __aenter__(self):
        from app.observability.request_metrics import current_request, record_stage

        self._admission_waiters += 1
        started = time.monotonic()
        try:
            result = await self._acquire()
            scope = self._scope.get()
            if scope is not None:
                self._active_modes[scope.mode] += 1
                scope.metrics_counted = True
            return result
        except Exception:
            if observation := current_request():
                observation.set_outcome("rejected")
            raise
        finally:
            self._admission_waiters -= 1
            record_stage("admission_wait", time.monotonic() - started)

    async def _acquire(self):
        from app.errors import UserLimitExceededError

        wait_started_ns = time.monotonic_ns()
        deadline = time.monotonic() + float(self._timeout)
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
            raise UserLimitExceededError(
                "Система перегружена большим количеством запросов. Пожалуйста, попробуйте еще раз через несколько минут."
            )

        self._waiting_count += 1
        try:
            # 1. Acquire local semaphore first (limits per-node concurrency to max limit)
            async with asyncio.timeout(_remaining(deadline)):
                await self._local_semaphore.__aenter__()
        except TimeoutError:
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
        self._token.set(token)
        self._acquired_ns.set(time.monotonic_ns())
        owner = asyncio.current_task()
        scope = _SemaphoreScope(token, owner, cancellation_count=owner.cancelling() if owner else 0)
        self._scope.set(scope)

        try:
            from app.cache import redis_client

            scope.redis = redis_client
            scope.mode = "distributed" if redis_client else "local_fallback"
            async with asyncio.timeout(_remaining(deadline)):
                if not redis_client:
                    emit(
                        "concurrency.acquire_finished",
                        operation="concurrency.acquire",
                        outcome="acquired",
                        mode="local_fallback",
                        degraded=True,
                        reason_code="redis_unavailable",
                        semaphore=self._key,
                        wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
                    )
                    return self

                while True:
                    _remaining(deadline)
                    started = time.monotonic()
                    try:
                        # Server time owns expiry; the client only bounds waiting.
                        admitted = await redis_client.eval(
                            _ADMIT_SLOT, 1, self._key, self._lease_ttl, self._limit, token
                        )
                    except Exception as error:
                        # Only initial infrastructure failure may degrade locally.
                        # Deadline expiry still rejects, even with socket retries.
                        _remaining(deadline)
                        scope.mode = "local_fallback"
                        emit(
                            "concurrency.acquire_finished",
                            level="warning",
                            operation="concurrency.acquire",
                            outcome="acquired",
                            mode="local_fallback",
                            degraded=True,
                            reason_code="redis_error",
                            semaphore=self._key,
                            error_type=type(error).__name__,
                            wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
                        )
                        return self
                    _remaining(deadline)
                    if admitted == 1:
                        scope.last_confirmed = started
                        scope.mode = "distributed"
                        try:
                            _remaining(started + self._lease_ttl)
                        except TimeoutError:
                            scope.lease_lost = True
                            raise
                        self._renewal.set(asyncio.create_task(self._renew_slot(redis_client, token)))
                        emit(
                            "concurrency.acquire_finished",
                            operation="concurrency.acquire",
                            outcome="acquired",
                            mode="distributed",
                            degraded=False,
                            semaphore=self._key,
                            wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
                        )
                        return self
                    await asyncio.sleep(min(0.5, _remaining(deadline)))
        except (TimeoutError, asyncio.CancelledError) as exc:
            # __aexit__ is not called by async-with when __aenter__ fails.
            # We already own the local slot and may have inserted a Redis token.
            try:
                emit(
                    "concurrency.acquire_finished",
                    level="warning",
                    operation="concurrency.acquire",
                    outcome="rejected",
                    mode="distributed",
                    reason_code=(
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "lease_lost"
                        if scope.lease_lost
                        else "global_timeout"
                    ),
                    semaphore=self._key,
                    wait_ms=round((time.monotonic_ns() - wait_started_ns) / 1_000_000, 2),
                )
            finally:
                await self.__aexit__(type(exc), exc, exc.__traceback__)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise UserLimitExceededError(
                "Система перегружена. Пожалуйста, повторите запрос немного позже.",
                details={"reason_code": "lease_lost"} if scope.lease_lost else None,
            ) from None
        except BaseException as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        scope = self._scope.get()

        async def release_distributed() -> None:
            try:
                async with asyncio.timeout(self._cleanup_timeout):
                    renewal = self._renewal.get()
                    if renewal is not None:
                        renewal.cancel()
                        await asyncio.gather(renewal, return_exceptions=True)
                    if scope is not None and scope.redis:
                        await scope.redis.zrem(self._key, scope.token)
            except Exception as e:
                logging.warning("Semaphore release unavailable key=%s type=%s", self._key, type(e).__name__)

        # Caller cancellation must not abandon the bounded release attempt.
        # This request-scoped child is always awaited, including on timeout.
        cleanup = asyncio.create_task(release_distributed())
        cancelled = exc_val if isinstance(exc_val, asyncio.CancelledError) else None
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as error:
                    if cancelled is None or (scope is not None and cancelled.args == (scope.cancellation_marker,)):
                        cancelled = error
            # Retrieve any unexpected failure rather than orphaning the child.
            cleanup.result()
        finally:
            self._renewal.set(None)
            self._local_semaphore.release()
            acquired_ns = self._acquired_ns.get()
            self._token.set(None)
            self._acquired_ns.set(None)
            self._scope.set(None)
            if scope is not None and scope.metrics_counted:
                self._active_modes[scope.mode] -= 1
                scope.metrics_counted = False
            emit(
                "concurrency.released",
                level="warning" if exc_type is not None or (scope and scope.lease_lost) else "debug",
                operation="concurrency.release",
                semaphore=self._key,
                outcome="released",
                mode=scope.mode if scope else None,
                degraded=scope.mode == "local_fallback" if scope else None,
                reason_code="lease_lost" if scope and scope.lease_lost else None,
                hold_ms=(round((time.monotonic_ns() - acquired_ns) / 1_000_000, 2) if acquired_ns else None),
                scope_error_type=exc_type.__name__ if exc_type is not None else None,
            )
        if scope and scope.lease_lost:
            # Consume only our cancellation, preserving independent caller or
            # shutdown cancellation and its original exception/message.
            internal_cancel = cancelled is not None and cancelled.args == (scope.cancellation_marker,)
            caller_cancelled = (
                scope.cancellation_sent
                and scope.owner is not None
                and scope.owner.uncancel() > scope.cancellation_count
            )
            if (
                internal_cancel
                and not caller_cancelled
                and exc_val is not None
                and not isinstance(exc_val, asyncio.CancelledError)
            ):
                # The provider failed while a ready renewal was losing its token.
                # Retain that original body failure instead of our exit marker.
                cancelled = None
            if (
                internal_cancel
                and not caller_cancelled
                and (exc_val is None or isinstance(exc_val, asyncio.CancelledError))
            ) or (cancelled is None and exc_val is None):
                from app.errors import UserLimitExceededError

                raise UserLimitExceededError(
                    "Система перегружена. Пожалуйста, повторите запрос немного позже.",
                    details={"reason_code": "lease_lost"},
                ) from None
        if cancelled is not None:
            raise cancelled


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

    def metrics_snapshot(self) -> dict[str, Any]:
        if self._inner is None:
            return {
                "scope": "process",
                "available": True,
                "observed_at": time.time(),
                "initialized": False,
                "waiters": 0,
                "active": 0,
                "degraded": False,
            }
        return self._inner.metrics_snapshot()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._inner is not None:
            await self._inner.__aexit__(exc_type, exc_val, exc_tb)


heavy_request_semaphore = _LazyGlobalLLMSemaphore("MAX_CONCURRENT_HEAVY_REQUESTS", redis_key="llm_req_semaphore")
ultra_heavy_semaphore = _LazyGlobalLLMSemaphore(
    "MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS", redis_key="llm_ultra_heavy_semaphore"
)
