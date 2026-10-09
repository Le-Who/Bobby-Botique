"""
Tests for the typed exception hierarchy in app.errors.

Verifies:
- Exception inheritance chain
- ConfigurationError raised when DATABASE_URL missing
- DatabaseRateLimitError raised on rate limit
- DatabaseConnectionError raised on connection failure
- DatabasePoolError raised on pool failure
- convert_to_typed_exception utility
"""

from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.errors import (
    APIError,
    APIQuotaExceededError,
    CacheError,
    CircuitBreakerOpenError,
    ConfigurationError,
    ConnectionTimeoutError,
    DatabaseConnectionError,
    DatabaseError,
    DatabasePoolError,
    DatabaseQueryError,
    DatabaseRateLimitError,
    GemaibotAPIError,
    GemaibotBaseException,
    InputSanitizationError,
    NetworkError,
    RedisConnectionError,
    SecurityError,
    convert_to_typed_exception,
)

# ─── Hierarchy Tests ─────────────────────────────────────────────────────────


class TestExceptionHierarchy:
    """Verify the class hierarchy is correct."""

    def test_database_errors_inherit_from_base(self):
        assert issubclass(DatabaseError, GemaibotBaseException)
        assert issubclass(DatabaseConnectionError, DatabaseError)
        assert issubclass(DatabaseQueryError, DatabaseError)
        assert issubclass(DatabaseRateLimitError, DatabaseError)
        assert issubclass(DatabasePoolError, DatabaseError)

    def test_api_errors_inherit_from_base(self):
        assert issubclass(GemaibotAPIError, GemaibotBaseException)
        assert issubclass(APIError, GemaibotAPIError)

    def test_network_errors_inherit_from_base(self):
        assert issubclass(NetworkError, GemaibotBaseException)
        assert issubclass(ConnectionTimeoutError, NetworkError)
        assert issubclass(CircuitBreakerOpenError, NetworkError)

    def test_cache_errors_inherit_from_base(self):
        assert issubclass(CacheError, GemaibotBaseException)
        assert issubclass(RedisConnectionError, CacheError)

    def test_security_errors_inherit_from_base(self):
        assert issubclass(SecurityError, GemaibotBaseException)
        assert issubclass(InputSanitizationError, SecurityError)

    def test_configuration_error_inherits_from_validation(self):
        from app.errors import ValidationError

        assert issubclass(ConfigurationError, ValidationError)
        assert issubclass(ConfigurationError, GemaibotBaseException)


# ─── Exception Construction Tests ─────────────────────────────────────────────


class TestExceptionConstruction:
    """Verify exceptions carry message and details correctly."""

    def test_base_exception_with_details(self):
        exc = GemaibotBaseException("test error", {"key": "value"})
        assert exc.message == "test error"
        assert exc.details == {"key": "value"}
        assert "key" in str(exc)

    def test_base_exception_without_details(self):
        exc = GemaibotBaseException("simple error")
        assert exc.details == {}
        assert str(exc) == "simple error"

    def test_database_rate_limit_error(self):
        exc = DatabaseRateLimitError("quota exceeded")
        assert isinstance(exc, DatabaseError)
        assert isinstance(exc, GemaibotBaseException)
        assert "quota exceeded" in str(exc)

    def test_configuration_error(self):
        exc = ConfigurationError("DATABASE_URL not set")
        assert "DATABASE_URL not set" in str(exc)

    def test_api_error_with_user_message(self):
        exc = APIError("503 Service Unavailable", retryable=True)
        assert exc.retryable is True
        assert exc.key_related is False
        assert "перегружен" in exc.user_message  # Overloaded message

    def test_api_error_from_exception(self):
        original = RuntimeError("quota exceeded for model")
        exc = APIError.from_exception(original)
        assert exc.key_related is True
        assert exc.retryable is False


# ─── convert_to_typed_exception Tests ────────────────────────────────────────


class TestConvertToTypedException:
    """Verify the conversion utility maps exceptions correctly."""

    def test_asyncpg_connection_error(self):
        original = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")
        result = convert_to_typed_exception(original, "db_init")
        assert type(result) is DatabaseConnectionError
        assert result.details == {"original_error": "ConnectionDoesNotExistError", "context": "db_init"}

    def test_rate_limit_maps_to_database_rate_limit(self):
        original = asyncpg.TooManyConnectionsError("rate limit exceeded")
        result = convert_to_typed_exception(original, "query")
        assert type(result) is DatabaseRateLimitError
        assert result.details == {"original_error": "TooManyConnectionsError", "context": "query"}

    def test_api_quota_maps_to_api_quota_exceeded_error(self):
        original = Exception("API quota exceeded")
        result = convert_to_typed_exception(original, "gemini_call")
        assert type(result) is APIQuotaExceededError
        assert result.details == {"original_error": "Exception", "context": "gemini_call"}

    def test_unknown_error_maps_to_base(self):
        original = ValueError("some random error")
        result = convert_to_typed_exception(original, "unknown")
        assert type(result) is GemaibotBaseException
        assert "Unexpected error" in str(result)

    def test_asyncpg_application_subclass_retains_database_ownership(self):
        class ApplicationConnectionError(asyncpg.ConnectionDoesNotExistError):
            pass

        original = ApplicationConnectionError("connection was closed in the middle of operation")
        result = convert_to_typed_exception(original, "application_db")
        assert type(result) is DatabaseConnectionError
        assert result.details == {"original_error": "ApplicationConnectionError", "context": "application_db"}

    def test_postgres_message_fallback_still_classifies_generic_exception(self):
        original = RuntimeError("postgres connection refused")
        result = convert_to_typed_exception(original, "legacy_db")
        assert type(result) is DatabaseConnectionError
        assert result.details == {"original_error": "RuntimeError", "context": "legacy_db"}


# ─── Database Layer Integration Tests ────────────────────────────────────────


class TestDatabaseTypedExceptions:
    """Verify database.py raises typed exceptions under the right conditions."""

    @pytest.mark.asyncio
    async def test_init_db_raises_configuration_error(self):
        """init_db() should raise ConfigurationError when DATABASE_URL is missing."""
        with patch("app.database.settings") as mock_settings:
            mock_settings.DATABASE_URL = None
            from app.database import init_db

            with pytest.raises(ConfigurationError, match="DATABASE_URL not set"):
                await init_db()

    @pytest.mark.asyncio
    async def test_create_pool_raises_rate_limit_error(self):
        """create_pool() should raise DatabaseRateLimitError on rate limit."""
        from app.database import DatabaseManager

        manager = DatabaseManager.__new__(DatabaseManager)
        manager.pool = None
        manager._monitor_task = None
        manager._reconnect_task = None
        manager._consecutive_failures = 0

        with patch("app.database.asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool.side_effect = Exception("rate limit exceeded")
            with pytest.raises(DatabaseRateLimitError):
                await manager.create_pool()

    @pytest.mark.asyncio
    async def test_create_pool_raises_connection_error(self):
        """create_pool() should raise DatabaseConnectionError on connection failure."""
        from app.database import DatabaseManager

        manager = DatabaseManager.__new__(DatabaseManager)
        manager.pool = None
        manager._monitor_task = None
        manager._reconnect_task = None
        manager._consecutive_failures = 0

        with patch("app.database.asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool.side_effect = Exception("connection refused timeout")
            with pytest.raises(DatabaseConnectionError):
                await manager.create_pool()

    @pytest.mark.asyncio
    async def test_create_pool_raises_pool_error_on_unknown(self):
        """create_pool() should raise DatabasePoolError on unknown errors."""
        from app.database import DatabaseManager

        manager = DatabaseManager.__new__(DatabaseManager)
        manager.pool = None
        manager._monitor_task = None
        manager._reconnect_task = None
        manager._consecutive_failures = 0

        with patch("app.database.asyncpg.create_pool", new_callable=AsyncMock) as mock_pool:
            mock_pool.side_effect = Exception("some random error")
            with pytest.raises(DatabasePoolError):
                await manager.create_pool()
