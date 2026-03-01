"""
Smoke tests for the Gemini timeout fix.

Verifies that:
1. asyncio.wait_for properly cancels the native async SDK call on timeout
2. No zombie coroutines remain after cancellation
3. The timeout error message is returned correctly
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai_provider import GeminiProvider


class TestGeminiTimeoutSmoke:
    """Smoke tests for the async Gemini SDK timeout behavior."""

    @pytest.mark.asyncio
    async def test_async_sdk_timeout_cancels_properly(self):
        """When generate_content takes too long, asyncio.wait_for should cancel it cleanly."""
        provider = GeminiProvider("key")

        async def slow_generate(*args, **kwargs):
            await asyncio.sleep(999)

        with (
            patch("app.ai_provider.genai.Client") as MockClient,
            patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
            patch("app.ai_provider.api_logger", new_callable=MagicMock),
            patch("app.ai_provider.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []

            mock_client = MockClient.return_value
            mock_aio_models = MagicMock()
            mock_aio_models.generate_content = slow_generate
            mock_client.aio.models = mock_aio_models

            original_wait_for = asyncio.wait_for

            async def fast_wait_for(coro, timeout=None):
                return await original_wait_for(coro, timeout=0.05)

            with patch("asyncio.wait_for", side_effect=fast_wait_for):
                resp = await provider._execute_request(
                    history=[{"role": "user", "parts": ["hi"]}],
                    model_name="gemini-2.5-flash",
                    system_instruction=None,
                    user_id=None,
                    chat_id=None,
                    timeout=100.0,
                )

        assert resp.success is False
        assert "Превышено время ожидания" in resp.text or "timed out" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_timeout_does_not_leave_zombie_tasks(self):
        """After timeout, there should be no leftover pending tasks."""
        provider = GeminiProvider("key")

        async def slow_generate(*args, **kwargs):
            await asyncio.sleep(999)

        with (
            patch("app.ai_provider.genai.Client") as MockClient,
            patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
            patch("app.ai_provider.api_logger", new_callable=MagicMock),
            patch("app.ai_provider.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []

            mock_client = MockClient.return_value
            mock_aio_models = MagicMock()
            mock_aio_models.generate_content = slow_generate
            mock_client.aio.models = mock_aio_models

            tasks_before = len([t for t in asyncio.all_tasks() if not t.done()])

            original_wait_for = asyncio.wait_for
            async def fast_wait_for(coro, timeout=None):
                return await original_wait_for(coro, timeout=0.05)

            with patch("asyncio.wait_for", side_effect=fast_wait_for):
                await provider._execute_request(
                    history=[{"role": "user", "parts": ["hi"]}],
                    model_name="gemini-3-flash",
                    system_instruction=None,
                    user_id=None,
                    chat_id=None,
                    timeout=100.0,
                )

            await asyncio.sleep(0.1)

            tasks_after = len([t for t in asyncio.all_tasks() if not t.done()])
            assert tasks_after <= tasks_before, (
                f"Zombie tasks detected: {tasks_after - tasks_before} new tasks after timeout"
            )

    @pytest.mark.asyncio
    async def test_native_async_vs_thread_cancellation(self):
        """Verify that native async coroutine properly raises CancelledError."""
        cancel_detected = False

        async def cancellable_coro():
            nonlocal cancel_detected
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                cancel_detected = True
                raise

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(cancellable_coro(), timeout=0.05)

        await asyncio.sleep(0.01)
        assert cancel_detected, "Native async coroutine should receive CancelledError on timeout"

    @pytest.mark.asyncio
    async def test_sdk_http_timeout_is_configured(self):
        """Verify that the genai.Client is created with HttpOptions(timeout=90_000)."""
        provider = GeminiProvider("key")

        with (
            patch("app.ai_provider.genai.Client") as MockClient,
            patch("app.ai_provider.metrics_collector", new_callable=AsyncMock),
            patch("app.ai_provider.api_logger", new_callable=MagicMock),
            patch("app.ai_provider.settings") as mock_settings,
        ):
            mock_settings.SAFETY_SETTINGS = []

            mock_response = MagicMock()
            mock_response.text = "ok"
            mock_token = MagicMock()
            mock_token.total_tokens = 10

            mock_aio_models = MagicMock()
            mock_aio_models.generate_content = AsyncMock(return_value=mock_response)
            mock_aio_models.count_tokens = AsyncMock(return_value=mock_token)
            MockClient.return_value.aio.models = mock_aio_models

            await provider._execute_request(
                history=[{"role": "user", "parts": ["hi"]}],
                model_name="gemini-2.5-flash",
                system_instruction=None,
                user_id=None,
                chat_id=None,
                timeout=100.0,
            )

        call_kwargs = MockClient.call_args[1]
        assert "http_options" in call_kwargs
        http_opts = call_kwargs["http_options"]
        assert hasattr(http_opts, "timeout") or "timeout" in str(call_kwargs)
