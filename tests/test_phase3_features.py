# tests/test_phase3_features.py
"""Integration tests for Phase 3 & 4 features: streaming, memory, model selector, GDPR, Prometheus."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.model_selector import _get_tier

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
                "gemini-3.1-flash-lite",
                "gemini-3.5-flash",
            ]
            result = select_model("Привет!", current_model="gemini-3.5-flash")
            # No downgrade suggestion — pro → flash is a downgrade
            assert result is None

    def test_code_message_suggests_top_tier(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
            ]
            result = select_model(
                "Напиши функцию для сортировки массива и исправь баг",
                current_model="gemini-3.1-flash-lite",
            )
            assert result is not None
            assert _get_tier(result.model) > 1  # Should suggest upgrade from lite

    def test_reasoning_message_suggests_top_tier(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
            ]
            result = select_model(
                "Объясни подробно, как работает алгоритм Дейкстры и в чём разница с A*",
                current_model="gemini-3.1-flash-lite",
            )
            assert result is not None

    def test_no_suggestion_when_model_already_optimal(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
            ]
            result = select_model("Привет!", current_model="gemini-3.1-flash-lite")
            assert result is None

    def test_no_suggestion_for_medium_message(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = [
                "gemini-3.5-flash",
                "gemini-3.1-flash-lite",
            ]
            result = select_model(
                "Расскажи о погоде в Москве сегодня",
                current_model="gemini-3.1-flash-lite",
            )
            # No strong signal → should return None
            assert result is None

    def test_no_models_available(self):
        from app.model_selector import select_model

        with patch("app.model_selector.settings") as mock_settings:
            mock_settings.AVAILABLE_MODELS = []
            result = select_model("test", current_model="gemini-3.1-flash-lite")
            assert result is None


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
        mock_update.effective_chat.type = "private"
        mock_update.message = AsyncMock()
        mock_update.message.reply_document = AsyncMock()

        mock_chat_state = MagicMock()
        mock_chat_state.model = "gemini-3.5-flash"
        mock_chat_state.thinking_level = "medium"
        mock_chat_state.search_enabled = True
        mock_chat_state.history = [{"role": "user", "parts": ["test"]}]
        mock_chat_state.token_count = 100
        mock_chat_state.system_prompt = "A private custom prompt"
        mock_chat_state.context_summary = "Prior context summary"
        mock_chat_state.ltm_enabled = True
        mock_chat_state.memory_epoch = 4
        mock_chat_state.branch_id = "branch-1"
        mock_chat_state.temperature = 0.7
        mock_chat_state.voice_id = "Aoede"
        mock_chat_state.tts_temperature = 0.5
        mock_chat_state.live_voice_name = "Puck"
        mock_chat_state.live_thinking_level = "low"
        mock_chat_state.live_connection_mode = "websocket"
        mock_chat_state.is_deep_dive = False
        mock_chat_state.deep_dive_thread_id = None

        with (
            patch("app.handlers.commands.get_user_chat", return_value=mock_chat_state),
            patch("app.handlers.commands.get_conversation_count", return_value=5),
            patch(
                "app.repos.conversations.export_user_conversations",
                return_value=[
                    {
                        "id": 71,
                        "title": "Saved chat",
                        "created_at": "2026-08-01",
                        "messages": [{"role": "user", "content": "saved message"}],
                    }
                ],
            ),
            patch(
                "app.repos.memory.export_user_memory",
                return_value={
                    "memories": [{"id": 11, "content": "likes tea"}],
                    "nodes": [{"id": 21, "entity_name": "tea"}],
                    "edges": [{"id": 31, "predicate": "likes"}],
                    "edge_sources": [{"edge_id": 31, "memory_id": 11}],
                },
            ) as export_memory,
        ):
            await mydata_command.__wrapped__.__wrapped__(mock_update, MagicMock())

        mock_update.message.reply_document.assert_called_once()
        call_args = mock_update.message.reply_document.call_args
        doc = call_args.kwargs.get("document") or call_args[1].get("document")
        assert doc is not None
        assert "граф" not in call_args.kwargs["caption"].lower()
        content = doc.read().decode("utf-8")
        data = json.loads(content)
        assert data["user_id"] == 12345
        assert data["current_model"] == "gemini-3.5-flash"
        assert data["chat_settings"]["system_prompt"] == "A private custom prompt"
        assert data["chat_settings"]["ltm_enabled"] is True
        assert data["active_conversation"] == [{"role": "user", "parts": ["test"]}]
        assert data["saved_conversations"][0]["messages"] == [{"role": "user", "content": "saved message"}]
        assert data["long_term_memory"]["memories"][0]["content"] == "likes tea"
        assert data["long_term_memory"]["nodes"][0]["entity_name"] == "tea"
        assert data["long_term_memory"]["edges"][0]["predicate"] == "likes"
        assert data["long_term_memory"]["edge_sources"] == [{"edge_id": 31, "memory_id": 11}]
        export_memory.assert_awaited_once_with(12345)

    @pytest.mark.asyncio
    async def test_mydata_refuses_to_export_private_archive_in_group(self):
        from app.handlers.commands import mydata_command

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.type = "group"
        update.message.reply_text = AsyncMock()
        update.message.reply_document = AsyncMock()

        with (
            patch("app.handlers.commands.get_user_chat", new_callable=AsyncMock) as get_chat,
            patch("app.repos.memory.export_user_memory", new_callable=AsyncMock) as export_memory,
            patch(
                "app.repos.conversations.export_user_conversations",
                new_callable=AsyncMock,
            ) as export_conversations,
        ):
            await mydata_command.__wrapped__.__wrapped__(update, MagicMock())

        update.message.reply_text.assert_awaited_once()
        update.message.reply_document.assert_not_awaited()
        get_chat.assert_not_awaited()
        export_memory.assert_not_awaited()
        export_conversations.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleteme_shows_confirmation(self):
        from app.handlers.commands import deleteme_command

        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        mock_update.effective_chat.type = "private"
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
