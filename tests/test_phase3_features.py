# tests/test_phase3_features.py
"""Integration tests for Phase 3 & 4 features: streaming, memory, model selector, GDPR, Prometheus."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# MODEL SELECTOR TESTS
# ============================================================================


class TestModelSelector:
    """Tests for app.model_selector."""

    def test_simple_message_no_downgrade(self):
        """Simple messages should NOT suggest a downgrade (e.g. pro → flash)."""
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-2.0-flash",
                "gemini-2.5-pro",
            ]
            result = select_model("Привет!", current_model="gemini-2.5-pro")
            # No downgrade suggestion — pro → flash is a downgrade
            assert result is None

    def test_code_message_suggests_pro(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-2.0-flash",
                "gemini-2.5-pro",
            ]
            result = select_model(
                "Напиши функцию для сортировки массива и исправь баг",
                current_model="gemini-2.0-flash",
            )
            assert result is not None
            assert "pro" in result.model.lower()

    def test_reasoning_message_suggests_pro(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-2.0-flash",
                "gemini-2.5-pro",
            ]
            result = select_model(
                "Объясни подробно, как работает алгоритм Дейкстры и в чём разница с A*",
                current_model="gemini-2.0-flash",
            )
            assert result is not None

    def test_no_suggestion_when_model_already_optimal(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-2.0-flash",
                "gemini-2.5-pro",
            ]
            result = select_model("Привет!", current_model="gemini-2.0-flash")
            assert result is None

    def test_no_suggestion_for_medium_message(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-2.0-flash",
                "gemini-2.5-pro",
            ]
            result = select_model(
                "Расскажи о погоде в Москве сегодня",
                current_model="gemini-2.0-flash",
            )
            # No strong signal → should return None
            assert result is None

    def test_no_models_available(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = []
            result = select_model("test", current_model="gemini-2.0-flash")
            assert result is None


# ============================================================================
# STREAMING TESTS
# ============================================================================


class TestStreamingWriter:
    """Tests for StreamingWriter debouncing logic."""

    @pytest.mark.asyncio
    async def test_writer_accumulates_text(self):
        from app.adapters.ui_adapter import StreamingUIAdapter
        from app.streaming import StreamingWriter

        mock_msg = AsyncMock()
        mock_msg.message_id = 99
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 123
        
        mock_adapter = AsyncMock(spec=StreamingUIAdapter)
        mock_adapter._bot = None

        writer = StreamingWriter(mock_adapter, chat_type="private")
        writer._debounce_s = 0.0
        writer._min_chunk = 0

        await writer.write("Hello ")
        await writer.write("World!")
        result = await writer.finalize()
        assert result == "Hello World!"

    @pytest.mark.asyncio
    async def test_writer_debounce_prevents_rapid_edits(self):
        from app.adapters.ui_adapter import StreamingUIAdapter
        from app.streaming import StreamingWriter

        mock_msg = AsyncMock()
        mock_msg.message_id = 99
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 123
        
        mock_adapter = AsyncMock(spec=StreamingUIAdapter)
        mock_adapter._bot = None
        
        writer = StreamingWriter(mock_adapter, chat_type="private")
        writer._debounce_s = 10.0  # Very high debounce
        writer._min_chunk = 0

        # Rapid writes should NOT trigger edit (due to debounce)
        for i in range(5):
            await writer.write(f"chunk{i} ")

        # Only finalize should trigger edit
        result = await writer.finalize()
        assert "chunk0" in result
        assert mock_adapter.edit_message.call_count >= 1

    @pytest.mark.asyncio
    async def test_writer_edit_count(self):
        from app.adapters.ui_adapter import StreamingUIAdapter
        from app.streaming import StreamingWriter

        mock_msg = AsyncMock()
        mock_msg.message_id = 99
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 123
        
        mock_adapter = AsyncMock(spec=StreamingUIAdapter)
        mock_adapter._bot = None

        writer = StreamingWriter(mock_adapter, chat_type="private")
        writer._debounce_s = 0.0
        writer._min_chunk = 0

        await writer.write("a" * 100)
        await writer.finalize()
        assert writer.edit_count >= 1

    @pytest.mark.asyncio
    async def test_stream_and_display_returns_text(self):
        from app.streaming import stream_and_display

        mock_msg = AsyncMock()
        mock_msg.message_id = 1
        mock_msg.chat = MagicMock()
        mock_msg.chat.id = 123
        mock_msg.get_bot = MagicMock(return_value=None)
        mock_msg.chat.type = "private"

        async def mock_stream(*a, **kw):
            for word in ["Hello", " ", "World"]:
                yield word

        # Since stream_and_display instantiates ProviderRouter itself or gets from getter,
        # we can patch the ProviderRouter class's stream_response method directly.
        with patch("app.providers.router.ProviderRouter.stream_response", side_effect=mock_stream):
            with patch("app.streaming.metrics_collector") as mock_mc:
                mock_mc.record_api_call = AsyncMock()
                text, success, last_msg = await stream_and_display(
                    mock_msg, "model", [], MagicMock(), chat_id=123, chat_type="private", bot=None
                )
                assert success
                assert "HelloWorld" in text.replace(" ", "")
                assert last_msg is not None


# ============================================================================
# PROMETHEUS TESTS
# ============================================================================


class TestPrometheus:
    """Tests for Prometheus metric exporter."""

    def test_generates_valid_text(self):
        with patch("app.prometheus.metrics_collector") as mock_mc:
            mock_mc._start_time = time.time() - 3600
            mock_mc._api_calls = {("gemini", "2.5-pro"): 42}
            mock_mc._errors = {"timeout": 3}

            from app.prometheus import generate_metrics_text

            text = generate_metrics_text()
            assert "gembot_uptime_seconds" in text
            assert "gembot_api_calls_total" in text
            assert "42" in text
            assert "# TYPE" in text
            assert "# HELP" in text

    def test_valid_prometheus_format(self):
        with patch("app.prometheus.metrics_collector") as mock_mc:
            mock_mc._start_time = time.time()
            mock_mc._api_calls = {}
            mock_mc._errors = {}

            from app.prometheus import generate_metrics_text

            text = generate_metrics_text()
            lines = text.strip().split("\n")
            # Every non-empty line should be either a comment (#) or a metric
            for line in lines:
                if not line.strip():
                    continue
                assert line.startswith("#") or " " in line, f"Bad line: {line}"


# ============================================================================
# GDPR COMMAND TESTS
# ============================================================================


class TestGDPRCommands:
    """Tests for /mydata and /deleteme commands."""

    @pytest.mark.asyncio
    async def test_mydata_returns_json_document(self):
        from app.handlers.commands import mydata_command

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_user.username = "testuser"
        mock_update.message = AsyncMock()
        mock_update.message.reply_document = AsyncMock()

        mock_chat_state = MagicMock()
        mock_chat_state.model = "gemini-2.5-pro"
        mock_chat_state.thinking_level = "medium"
        mock_chat_state.search_enabled = True
        mock_chat_state.history = [{"role": "user", "parts": ["test"]}]
        mock_chat_state.token_count = 100
        mock_chat_state.system_prompt = None

        with (
            patch("app.handlers.commands.get_user_chat", return_value=mock_chat_state),
            patch("app.handlers.commands.get_conversation_count", return_value=5),
        ):
            await mydata_command.__wrapped__.__wrapped__(mock_update, MagicMock())

        mock_update.message.reply_document.assert_called_once()
        call_args = mock_update.message.reply_document.call_args
        doc = call_args.kwargs.get("document") or call_args[1].get("document")
        assert doc is not None
        content = doc.read().decode("utf-8")
        data = json.loads(content)
        assert data["user_id"] == 12345
        assert data["current_model"] == "gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_deleteme_shows_confirmation(self):
        from app.handlers.commands import deleteme_command

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.message = AsyncMock()
        mock_update.message.text = "/deleteme"
        mock_update.message.reply_text = AsyncMock()

        with patch("app.handlers.commands.TelegramFormatter") as mock_fmt:
            mock_fmt.format_text.return_value = ("formatted", "Markdown")
            await deleteme_command.__wrapped__.__wrapped__(mock_update, MagicMock())

        mock_update.message.reply_text.assert_called_once()
        _ = mock_update.message.reply_text.call_args
        assert "CONFIRM" in str(mock_fmt.format_text.call_args)


# ============================================================================
# DEGRADATION MATRIX TESTS
# ============================================================================


class TestDegradation:
    """Tests for degradation.py health checks."""

    def test_service_status_enum(self):
        from app.degradation import ServiceStatus

        assert ServiceStatus.HEALTHY.value == "healthy"
        assert ServiceStatus.DEGRADED.value == "degraded"
        assert ServiceStatus.UNAVAILABLE.value == "unavailable"

    def test_system_health_overall_healthy(self):
        from app.degradation import ServiceStatus, SystemHealth

        health = SystemHealth()
        assert health.overall == ServiceStatus.HEALTHY

    def test_system_health_degraded_when_redis_down(self):
        from app.degradation import ServiceStatus, SystemHealth

        health = SystemHealth(redis=ServiceStatus.UNAVAILABLE)
        assert health.overall == ServiceStatus.DEGRADED

    def test_can_process_message_healthy(self):
        from app.degradation import SystemHealth, can_process_message

        health = SystemHealth()
        can, msg = can_process_message(health)
        assert can is True
        assert msg is None

    def test_can_process_message_db_down(self):
        from app.degradation import ServiceStatus, SystemHealth, can_process_message

        health = SystemHealth(database=ServiceStatus.UNAVAILABLE)
        can, msg = can_process_message(health)
        assert can is False
        assert msg is not None
        assert "базы данных" in msg

    def test_to_dict(self):
        from app.degradation import SystemHealth

        health = SystemHealth()
        d = health.to_dict()
        assert d["overall"] == "healthy"
        assert d["database"] == "healthy"
