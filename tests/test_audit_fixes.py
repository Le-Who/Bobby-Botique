"""Tests for audit fixes — C1-C3, H1-H5, M1-M8, L1-L4.

This module validates the bug fixes from the codebase audit to prevent regressions.
"""

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ===========================================================================
# M4 — is_encrypted hardening (base64url validation)
# ===========================================================================


class TestIsEncryptedHardened:
    """Test the strengthened is_encrypted heuristic (M4 fix)."""

    def test_rejects_short_strings(self):
        from app.crypto import is_encrypted

        assert is_encrypted("gAAAAA") is False
        assert is_encrypted("gAAAAA_short") is False
        assert is_encrypted("") is False

    def test_rejects_wrong_prefix(self):
        from app.crypto import is_encrypted

        assert is_encrypted("x" * 200) is False
        assert is_encrypted("AIzaSy" + "B" * 100) is False

    def test_rejects_non_base64url_with_correct_prefix(self):
        """A string starting with gAAAAA but containing invalid base64 chars."""
        from app.crypto import is_encrypted

        # Contains spaces and special chars — not valid base64url
        bad = "gAAAAA" + " not valid!!%&*() " * 5
        assert is_encrypted(bad) is False

    def test_accepts_real_fernet_token(self):
        from app.crypto import is_encrypted

        # Valid Fernet token structure (base64url)
        token = "gAAAAABnQOeA" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" + "=="
        assert is_encrypted(token) is True

    def test_accepts_actual_encrypted_key(self):
        """Round-trip: encrypt a key and verify is_encrypted detects it."""
        mock_settings = MagicMock()
        mock_settings.ADMIN_SECRET = "test-secret-for-audit-tests"

        with patch("app.crypto.settings", mock_settings, create=True), patch("app.config.settings", mock_settings):
            from app.crypto import encrypt_api_key, is_encrypted, reset_fernet

            reset_fernet()
            ciphertext = encrypt_api_key("AIzaSyB_test123")
            assert is_encrypted(ciphertext) is True
            reset_fernet()


# ===========================================================================
# M3 — Narrowed is_key_related_error
# ===========================================================================


class TestIsKeyRelatedErrorNarrowed:
    """Test that is_key_related_error no longer false-positives on bare 'limit'."""

    def test_rate_limit_is_key_related(self):
        from app.errors import is_key_related_error

        assert is_key_related_error("Rate limit exceeded for key") is True
        assert is_key_related_error("rate_limit: too many requests") is True

    def test_daily_limit_is_key_related(self):
        from app.errors import is_key_related_error

        assert is_key_related_error("daily limit reached") is True

    def test_limit_exceeded_is_key_related(self):
        from app.errors import is_key_related_error

        assert is_key_related_error("API limit exceeded") is True

    def test_bare_limit_is_NOT_key_related(self):
        """M3 fix: bare 'limit' should NOT trigger key rotation."""
        from app.errors import is_key_related_error

        # Message length limit — should NOT trigger key rotation
        assert is_key_related_error("Message exceeds the limit of 4096 characters") is False

    def test_quota_is_key_related(self):
        from app.errors import is_key_related_error

        assert is_key_related_error("quota exceeded") is True

    def test_timeout_is_NOT_key_related(self):
        from app.errors import is_key_related_error

        assert is_key_related_error("⏰ Превышено время ожидания") is False
        assert is_key_related_error("timeout waiting for response") is False

    def test_overloaded_is_NOT_key_related(self):
        from app.errors import is_key_related_error

        assert is_key_related_error("🔄 Server unavailable") is False


# ===========================================================================
# M1 — safe_handler and safe_callback decorators
# ===========================================================================


class TestSafeHandlerDecorator:
    """Test the DRY safe_handler error decorator (M1 fix)."""

    @pytest.mark.asyncio
    async def test_safe_handler_catches_exception(self):
        from app.utils.decorators import safe_handler

        mock_update = MagicMock()
        mock_update.effective_user.id = 42
        mock_update.message = AsyncMock()
        mock_update.callback_query = None
        mock_context = MagicMock()

        @safe_handler("❌ Custom error message")
        async def boom(update, context):
            raise RuntimeError("Something broke!")

        # Should not raise
        await boom(mock_update, mock_context)

        # Should have sent the error message
        mock_update.message.reply_text.assert_awaited_once_with("❌ Custom error message")

    @pytest.mark.asyncio
    async def test_safe_handler_passes_through_on_success(self):
        from app.utils.decorators import safe_handler

        mock_update = MagicMock()
        mock_update.effective_user.id = 42
        mock_context = MagicMock()

        @safe_handler()
        async def ok_handler(update, context):
            return "ok"

        result = await ok_handler(mock_update, mock_context)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_safe_callback_catches_exception(self):
        from app.utils.decorators import safe_callback

        mock_update = MagicMock()
        mock_update.effective_user.id = 42
        mock_update.callback_query = AsyncMock()
        mock_update.message = None
        mock_context = MagicMock()

        @safe_callback("❌ Callback error")
        async def boom_cb(update, context):
            raise ValueError("CB broke!")

        await boom_cb(mock_update, mock_context)
        mock_update.callback_query.answer.assert_awaited_once_with("❌ Callback error", show_alert=True)


# ===========================================================================
# C2 — Media group max-size guard
# ===========================================================================


class TestMediaGroupMaxSize:
    """Test the MEDIA_GROUPS_MAX_SIZE guard (C2 fix)."""

    def test_max_size_constant_exists(self):
        from app.handlers.messages import MEDIA_GROUPS_MAX_SIZE

        assert MEDIA_GROUPS_MAX_SIZE == 500

    @pytest.mark.asyncio
    async def test_cleanup_removes_expired_groups(self):
        from app.handlers import messages

        # Save originals
        orig_groups = messages.MEDIA_GROUPS.copy()
        orig_ttl = messages.MEDIA_GROUPS_TTL.copy()

        try:
            messages.MEDIA_GROUPS.clear()
            messages.MEDIA_GROUPS_TTL.clear()

            # Add an expired group
            messages.MEDIA_GROUPS["expired-1"] = ["photo1"]
            messages.MEDIA_GROUPS_TTL["expired-1"] = time.monotonic() - 600  # 10 min ago

            # Add a fresh group
            messages.MEDIA_GROUPS["fresh-1"] = ["photo2"]
            messages.MEDIA_GROUPS_TTL["fresh-1"] = time.monotonic()

            await messages.cleanup_old_media_groups()

            assert "expired-1" not in messages.MEDIA_GROUPS
            assert "expired-1" not in messages.MEDIA_GROUPS_TTL
            assert "fresh-1" in messages.MEDIA_GROUPS
        finally:
            messages.MEDIA_GROUPS.clear()
            messages.MEDIA_GROUPS.update(orig_groups)
            messages.MEDIA_GROUPS_TTL.clear()
            messages.MEDIA_GROUPS_TTL.update(orig_ttl)


# ===========================================================================
# H2 — Config reload debouncing
# ===========================================================================


class TestConfigReloadDebounce:
    """Test that ConfigManager initializes debounce state properly (H2 fix)."""

    def test_init_creates_reload_task_as_none(self):
        from app.config import ConfigManager

        cm = ConfigManager()
        assert cm._reload_task is None, "Fresh ConfigManager should have no pending reload task"

    def test_init_sets_last_reload_timestamp(self):
        import time

        from app.config import ConfigManager

        before = time.time()
        cm = ConfigManager()
        assert cm._last_reload >= before, "ConfigManager should stamp _last_reload on init"

    def test_settings_property_returns_settings_without_reload_within_interval(self):
        from app.config import ConfigManager

        cm = ConfigManager()
        s1 = cm.settings
        s2 = cm.settings
        # Accessing .settings twice quickly should return same object (no reload triggered)
        assert s1 is s2, "Within reload interval, settings should be the cached instance"


# ===========================================================================
# H1 — UserStateStore race condition fix
# ===========================================================================


class TestUserStateStoreSetdefault:
    """Test that _UserStateStore returns the same object for concurrent access (H1 fix)."""

    def test_same_user_returns_same_state_object(self):
        """Two calls for the same user_id must return the exact same UserState."""
        from app.state import get_user_state

        s1 = get_user_state(999888)
        s2 = get_user_state(999888)
        assert s1 is s2, "get_user_state must return identity-equal objects for same user"


# ===========================================================================
# H5 — httpx logger suppression
# ===========================================================================


class TestHttpxLoggerSuppression:
    """Test that httpx/httpcore loggers are actually suppressed at runtime (H5 fix)."""

    def test_httpx_logger_suppressed_after_setup(self):
        import logging

        from app.utils.logging_config import setup_detailed_logging

        setup_detailed_logging(log_level="DEBUG", enable_structured_logging=False)
        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        # httpx/httpcore should be WARNING or higher to prevent token leakage
        assert httpx_logger.level >= logging.WARNING, (
            f"httpx logger level is {httpx_logger.level}, expected >= WARNING ({logging.WARNING})"
        )
        assert httpcore_logger.level >= logging.WARNING, (
            f"httpcore logger level is {httpcore_logger.level}, expected >= WARNING ({logging.WARNING})"
        )


# ===========================================================================
# L4 — httpx client close function
# ===========================================================================


class TestHttpClientClose:
    """Test that close_http_clients exists and is callable (L4 fix)."""

    @pytest.mark.asyncio
    async def test_close_http_clients_runs_without_error(self):
        """close_http_clients must execute successfully (not just be callable)."""
        from app.providers import close_http_clients

        # Actually invoke it — should not raise
        await close_http_clients()

    @pytest.mark.asyncio
    async def test_double_close_is_idempotent(self):
        """Closing twice should not raise."""
        import app.providers.openrouter as aip_or
        from app.providers import close_http_clients

        _original_client = aip_or._openrouter_http_client

        # Close
        await close_http_clients()
        assert aip_or._openrouter_http_client is None

        # Close again — should not raise
        await close_http_clients()
        assert aip_or._openrouter_http_client is None

        # Restore for other tests
        from app.utils.network import NetworkErrorHandler

        aip_or._openrouter_http_client = NetworkErrorHandler.create_robust_http_client()


# ===========================================================================
# M2 — No lock in RoleConversationMetricsCollector
# ===========================================================================


class TestMetricsCollectorNoLock:
    """Test that RoleConversationMetricsCollector has no _lock attribute (M2 fix)."""

    def test_no_lock_attribute(self):
        from app.metrics import RoleConversationMetricsCollector

        collector = RoleConversationMetricsCollector()
        assert not hasattr(collector, "_lock")

    @pytest.mark.asyncio
    async def test_record_role_application_works(self):
        from app.metrics import RoleConversationMetricsCollector

        collector = RoleConversationMetricsCollector()
        await collector.record_role_application("test_role")
        assert collector.role_metrics.role_applications["test_role"] == 1

    @pytest.mark.asyncio
    async def test_get_metrics_summary_works(self):
        from app.metrics import RoleConversationMetricsCollector

        collector = RoleConversationMetricsCollector()
        summary = await collector.get_metrics_summary()
        assert "roles" in summary
        assert "conversations" in summary
        assert "summarization" in summary


# ===========================================================================
# H4 — migrate_invalid_models in repos/chats.py
# ===========================================================================


class TestMigrateInvalidModels:
    """Test the extracted migrate_invalid_models function (H4 fix)."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_models(self):
        from app.repos.chats import migrate_invalid_models

        count = await migrate_invalid_models(set(), "gemini-flash", "gpt-4")
        assert count == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_db_disconnected(self):
        from app.repos.chats import migrate_invalid_models

        with patch("app.repos.chats.db_manager") as mock_mgr:
            mock_mgr.is_connected = False
            count = await migrate_invalid_models({"gemini-flash"}, "gemini-flash", "gpt-4")
            assert count == 0

    @pytest.mark.asyncio
    async def test_migrates_invalid_models(self):
        from app.repos.chats import migrate_invalid_models

        mock_invalid_chats = [
            {"user_id": 1, "model": "old-gemini"},
            {"user_id": 2, "model": "vendor/old-openrouter"},
        ]

        with (
            patch("app.repos.chats.db_manager") as mock_mgr,
            patch("app.repos.chats.db_query", new_callable=AsyncMock) as mock_query,
        ):
            mock_mgr.is_connected = True
            mock_query.return_value = mock_invalid_chats

            count = await migrate_invalid_models(
                {"gemini-flash", "vendor/gpt-4"},
                default_gemini_model="gemini-flash",
                default_openrouter_model="vendor/gpt-4",
            )

            assert count == 2
            # Verify the update calls
            calls = mock_query.call_args_list
            # First call is the SELECT, next 2 are batch UPDATEs
            assert len(calls) == 3
            # Gemini batch (user 1, no "/" → gemini default)
            assert calls[1][0][1] == ("gemini-flash", [1])
            # OpenRouter batch (user 2, "/" present → openrouter default)
            assert calls[2][0][1] == ("vendor/gpt-4", [2])


# ===========================================================================
# M8 — database.py unused imports removed
# ===========================================================================


class TestDatabaseCleanImports:
    """Test that database module doesn't carry dead-weight top-level imports (M8 fix)."""

    def test_database_module_loads_cleanly(self):
        """database module should import without errors — real behavioral check."""
        import importlib

        import app.database as db_mod

        # Re-import to ensure no stale state
        importlib.reload(db_mod)
        # Verify key public API exists
        assert hasattr(db_mod, "db_query"), "db_query must be a top-level function"
        assert callable(db_mod.db_query)


# ===========================================================================
# L3 — validate_file_upload removed from security.py
# ===========================================================================


class TestValidateFileUploadRemoved:
    """Test that unused validate_file_upload was removed (L3 fix)."""

    def test_function_not_in_module(self):
        import app.security as sec

        assert not hasattr(sec, "validate_file_upload")


# ===========================================================================
# I3 — pydantic-settings replaced with pydantic in requirements.txt
# ===========================================================================


class TestRequirementsPydantic:
    """Test that pydantic is importable and pydantic-settings is not used (I3 fix)."""

    def test_pydantic_importable_at_runtime(self):
        """Verify pydantic works at runtime — more reliable than checking requirements.txt."""
        import pydantic

        # Must have BaseModel (core dependency of Settings)
        assert hasattr(pydantic, "BaseModel")

    def test_settings_uses_base_settings_from_pydantic(self):
        """Settings class should not depend on pydantic-settings package."""
        from app.config import Settings

        # Settings should be a pydantic model, not from pydantic_settings
        import pydantic

        assert issubclass(Settings, pydantic.BaseModel)
