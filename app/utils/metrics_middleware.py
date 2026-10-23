"""
Metrics middleware and decorator for automatic performance tracking.

Extracted from app.metrics to separate cross-cutting concerns from
the core MetricsCollector implementation.
"""

import asyncio
import functools
import time
from contextvars import ContextVar, Token

_operation_failure: ContextVar[bool] = ContextVar("user_operation_failure", default=False)


def mark_operation_failed() -> None:
    """Carry a handled terminal failure to the single user-operation owner."""
    _operation_failure.set(True)


def operation_failed() -> bool:
    """Read the current operation outcome without consuming an outer scope."""
    return _operation_failure.get()


def consume_operation_failure() -> bool:
    """Read and clear a handled failure when an ingress records its operation."""
    failed = _operation_failure.get()
    _operation_failure.set(False)
    return failed


class MetricsMiddleware:
    """Async context manager that records request timing and errors."""

    def __init__(self, func_name: str, *, user_id: int | None = None):
        self.func_name = func_name
        self.user_id = user_id
        self.success = True
        self._failure_token: Token[bool] | None = None

    async def __aenter__(self):
        self.start_time = time.time()
        if self.user_id is not None:
            self._failure_token = _operation_failure.set(False)
        return self

    async def __aexit__(self, exc_type, exc_val, _exc_tb):
        from app.metrics import metrics_collector

        try:
            if self.user_id is not None and exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
                return
            response_time = time.time() - self.start_time
            success = exc_type is None and self.success
            if self.user_id is not None:
                success = success and not operation_failed()
                await metrics_collector.record_request(self.func_name, response_time, success, user_id=self.user_id)
            else:
                await metrics_collector.record_request(self.func_name, response_time, success)

            if exc_type is not None:
                await metrics_collector.record_error(exc_type.__name__, str(exc_val) if exc_val else "Unknown error")
        finally:
            if self._failure_token is not None:
                _operation_failure.reset(self._failure_token)


def track_metrics(func_name: str):
    """Decorator for tracking function performance metrics."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            async with MetricsMiddleware(func_name):
                return await func(*args, **kwargs)

        return wrapper

    return decorator
