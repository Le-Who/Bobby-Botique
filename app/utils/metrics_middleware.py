"""
Metrics middleware and decorator for automatic performance tracking.

Extracted from app.metrics to separate cross-cutting concerns from
the core MetricsCollector implementation.
"""

import functools
import time


class MetricsMiddleware:
    """Async context manager that records request timing and errors."""

    def __init__(self, func_name: str):
        self.func_name = func_name

    async def __aenter__(self):
        self.start_time = time.time()
        return self

    async def __aexit__(self, exc_type, exc_val, _exc_tb):
        from app.metrics import metrics_collector

        response_time = time.time() - self.start_time
        success = exc_type is None

        await metrics_collector.record_request(self.func_name, response_time, success)

        if not success:
            await metrics_collector.record_error(
                exc_type.__name__ if exc_type else "Unknown",
                str(exc_val) if exc_val else "Unknown error",
            )


def track_metrics(func_name: str):
    """Decorator for tracking function performance metrics."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            async with MetricsMiddleware(func_name):
                return await func(*args, **kwargs)

        return wrapper

    return decorator
