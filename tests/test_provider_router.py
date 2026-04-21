"""
Tests for ProviderRouter and KeyStatusManager.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.providers import ProviderRouter

# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeKeyStatusManager:
    """In-memory KeyStatusManager that tracks suspended keys and recorded successes."""

    def __init__(self) -> None:
        self.suspended_keys: dict[str, dict] = {}  # key_hash → {'category': ..., 'error_text': ...}
        self.successful_keys: list[str] = []

    async def suspend_key(
        self,
        key_hash: str,
        model_name: str,
        error_category: str,
        error_text: str = "",
    ) -> None:
        self.suspended_keys[key_hash] = {
            "category": error_category,
            "error_text": error_text,
        }

    async def record_success(self, key_hash: str, model_name: str) -> None:
        if key_hash not in self.suspended_keys:
            self.successful_keys.append(key_hash)


class FakeAgentRequestUseCase:
    """Fake use case to control the responses for resolve/get/increment."""

    def __init__(self, resolve_sequence: list, response_sequence: list) -> None:
        self.resolve_sequence = list(resolve_sequence)
        self.response_sequence = list(response_sequence)
        self.resolves_made = 0
        self.responses_made = 0
        self.usages_incremented: list[str] = []

    async def resolve_ai_request(
        self, model_name: str, use_openrouter: bool = False, excluded_key_hashes: set[str] | None = None, **kwargs
    ) -> tuple[dict | None, str | None, str | None]:
        self.resolves_made += 1
        if self.resolve_sequence:
            return self.resolve_sequence.pop(0)
        return (None, None, "all_exhausted")

    async def get_ai_response(self, *args, **kwargs) -> tuple[str, int | None]:
        self.responses_made += 1
        return self.response_sequence.pop(0)

    async def increment_key_usage(self, key_hash: str, model_name: str, use_openrouter: bool = False) -> None:
        self.usages_incremented.append(key_hash)


# ── Tests for ProviderRouter.get_response ──────────────────────────────────────


class TestProviderRouter:
    @pytest.mark.asyncio
    async def test_successful_response(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
            ],
            response_sequence=[
                ("Hello!", 10),
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}])

        assert text == "Hello!"
        assert tokens == 10
        assert "hash1" in fake_status.successful_keys
        assert fake_use_case.usages_incremented == ["hash1"]

    @pytest.mark.asyncio
    async def test_all_keys_exhausted(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                (None, None, "all_exhausted"),
            ],
            response_sequence=[],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}])

        assert "🚫" in text
        assert tokens is None

    @pytest.mark.asyncio
    async def test_key_failure_triggers_retry_and_suspend(self):
        """Permanent key error should suspend key and trigger retry."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3.1", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("Hello!", 10),  # success
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response(
                "gemini-3.1", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert text == "Hello!"
        assert tokens == 10
        assert "hash1" in fake_status.suspended_keys
        assert fake_status.suspended_keys["hash1"]["category"] == "permanent"
        assert "hash2" not in fake_status.suspended_keys
        assert "hash2" in fake_status.successful_keys

    @pytest.mark.asyncio
    async def test_quota_error_suspends_with_quota_category(self):
        """Quota exceeded should suspend with 'quota' category."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                (None, None, "all_exhausted"),
            ],
            response_sequence=[
                ("🚫 Достигнут лимит запросов к API (Quota Exceeded).", None),
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}])

        assert "hash1" in fake_status.suspended_keys
        assert fake_status.suspended_keys["hash1"]["category"] == "quota"

    @pytest.mark.asyncio
    async def test_openrouter_detection(self):
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                (None, None, "all_exhausted"),
            ],
            response_sequence=[],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, _ = await router.get_response("openai/gpt-4o", [{"role": "user", "parts": ["hi"]}])

        assert "OpenRouter" in text

    @pytest.mark.asyncio
    async def test_transient_error_not_suspended(self):
        """503/timeout errors should NOT suspend the key."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3.1", None),
                (None, None, "all_exhausted"),
            ],
            response_sequence=[
                ("⏰ Превышено время ожидания ответа от API.", None),  # transient, from timeout
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            await router.get_response("gemini-3.1", [{"role": "user", "parts": ["hi"]}], max_key_retries=3)

        assert "hash1" not in fake_status.suspended_keys

    @pytest.mark.asyncio
    async def test_model_fallback_on_consecutive_permanent_errors(self):
        """If a given model has too many permanent errors in a row, the router falls back to the next model."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3-flash-preview", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3-flash-preview", None),
                ({"api_key": "k3", "key_hash": "hash3"}, "gemini-3-flash-preview", None),
                ({"api_key": "k4", "key_hash": "hash4"}, "gemini-2.5-flash", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🔑 Неверный API ключ.", None),  # permanent
                ("Fallback response!", 15),  # success on fallback model
            ],
        )

        mock_settings = MagicMock()
        mock_settings.AVAILABLE_MODELS = [
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-flash-latest",
        ]

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
            patch("app.providers.router.settings", mock_settings),
        ):
            text, tokens = await router.get_response(
                "gemini-3-flash-preview", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert text == "Fallback response!"
        assert tokens == 15

        # 3 keys from the first model should be suspended
        assert "hash1" in fake_status.suspended_keys
        assert "hash2" in fake_status.suspended_keys
        assert "hash3" in fake_status.suspended_keys

        assert "hash4" in fake_status.successful_keys

    @pytest.mark.asyncio
    async def test_no_model_fallback_on_non_permanent_errors(self, monkeypatch):
        """Model fallback should NOT trigger for quota/rate-limit errors."""
        router = ProviderRouter()
        fake_status = FakeKeyStatusManager()
        fake_use_case = FakeAgentRequestUseCase(
            resolve_sequence=[
                ({"api_key": "k1", "key_hash": "hash1"}, "gemini-3-flash-preview", None),
                ({"api_key": "k2", "key_hash": "hash2"}, "gemini-3-flash-preview", None),
                ({"api_key": "k3", "key_hash": "hash3"}, "gemini-3-flash-preview", None),
            ],
            response_sequence=[
                ("🔑 Неверный API ключ.", None),  # permanent
                ("🚫 Достигнут лимит запросов к API (Quota Exceeded).", None),  # quota (not permanent to model)
                ("🔑 Неверный API ключ.", None),  # permanent
            ],
        )

        with (
            patch("app.agent_use_cases.AgentRequestUseCase", return_value=fake_use_case),
            patch("app.repos.keys.get_key_status_manager", return_value=fake_status),
        ):
            text, tokens = await router.get_response(
                "gemini-3-flash-preview", [{"role": "user", "parts": ["hi"]}], max_key_retries=3
            )

        assert "🚫" in text
        assert tokens is None
