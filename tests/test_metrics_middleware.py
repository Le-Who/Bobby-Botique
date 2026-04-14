"""Tests for app.utils.metrics_middleware — MetricsMiddleware + track_metrics."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.utils.metrics_middleware import MetricsMiddleware, track_metrics

# ── MetricsMiddleware ────────────────────────────────────────────────────────
# metrics_collector is lazily imported inside __aexit__ from app.metrics,
# so we must patch at app.metrics.metrics_collector level.


class TestMetricsMiddleware:
    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_records_success(self, mock_mc):
        mock_mc.record_request = AsyncMock()
        async with MetricsMiddleware("test_op"):
            pass  # Success path
        mock_mc.record_request.assert_called_once()
        args, kwargs = mock_mc.record_request.call_args
        assert args[0] == "test_op"
        assert args[2] is True  # success

    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_records_failure_on_exception(self, mock_mc):
        mock_mc.record_request = AsyncMock()
        mock_mc.record_error = AsyncMock()
        with pytest.raises(ValueError):
            async with MetricsMiddleware("fail_op"):
                raise ValueError("boom")
        mock_mc.record_request.assert_called_once()
        args, kwargs = mock_mc.record_request.call_args
        assert args[2] is False  # success=False
        mock_mc.record_error.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_measures_duration(self, mock_mc):
        mock_mc.record_request = AsyncMock()
        async with MetricsMiddleware("timed_op"):
            await asyncio.sleep(0.05)
        args, kwargs = mock_mc.record_request.call_args
        duration = args[1]
        assert duration >= 0.04  # Allow small timing variance

    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_exception_not_swallowed(self, mock_mc):
        """MetricsMiddleware should re-raise exceptions (no __aexit__ return True)."""
        mock_mc.record_request = AsyncMock()
        mock_mc.record_error = AsyncMock()
        with pytest.raises(RuntimeError, match="test error"):
            async with MetricsMiddleware("op"):
                raise RuntimeError("test error")


# ── track_metrics decorator ──────────────────────────────────────────────────


class TestTrackMetrics:
    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_decorator_wraps_function(self, mock_mc):
        mock_mc.record_request = AsyncMock()

        @track_metrics("decorated_op")
        async def my_func(x, y):
            return x + y

        result = await my_func(3, 4)
        assert result == 7
        mock_mc.record_request.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_decorator_propagates_exception(self, mock_mc):
        mock_mc.record_request = AsyncMock()
        mock_mc.record_error = AsyncMock()

        @track_metrics("failing_op")
        async def my_func():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError, match="fail"):
            await my_func()

    @pytest.mark.asyncio
    @patch("app.metrics.metrics_collector")
    async def test_decorator_preserves_function_name(self, mock_mc):
        mock_mc.record_request = AsyncMock()

        @track_metrics("named_op")
        async def specific_function():
            pass

        # functools.wraps should preserve the name
        assert specific_function.__name__ == "specific_function"
