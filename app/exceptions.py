"""
Centralized exception handling system for the bot.
Provides typed exceptions for different error categories.
"""

from typing import Optional, Any, Dict


class GemaibotBaseException(Exception):
    """Base exception class for all bot-related errors."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
    
    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


# Database Exceptions
class DatabaseError(GemaibotBaseException):
    """Base class for database-related errors."""
    pass


class DatabaseConnectionError(DatabaseError):
    """Raised when database connection fails."""
    pass


class DatabaseQueryError(DatabaseError):
    """Raised when database query execution fails."""
    pass


class DatabaseRateLimitError(DatabaseError):
    """Raised when database rate limit is exceeded."""
    pass


class DatabasePoolError(DatabaseError):
    """Raised when database connection pool issues occur."""
    pass


# API Exceptions
class APIError(GemaibotBaseException):
    """Base class for API-related errors."""
    pass


class GeminiAPIError(APIError):
    """Raised when Gemini API calls fail."""
    pass


class TavilyAPIError(APIError):
    """Raised when Tavily API calls fail."""
    pass


class TelegramAPIError(APIError):
    """Raised when Telegram Bot API calls fail."""
    pass


class APIQuotaExceededError(APIError):
    """Raised when API quota is exceeded."""
    pass


class APIInvalidResponseError(APIError):
    """Raised when API returns invalid response."""
    pass


# Network Exceptions
class NetworkError(GemaibotBaseException):
    """Base class for network-related errors."""
    pass


class ConnectionTimeoutError(NetworkError):
    """Raised when connection times out."""
    pass


class ConnectionRefusedError(NetworkError):
    """Raised when connection is refused."""
    pass


class CircuitBreakerOpenError(NetworkError):
    """Raised when circuit breaker is open."""
    pass


# Validation Exceptions
class ValidationError(GemaibotBaseException):
    """Base class for validation errors."""
    pass


class InputValidationError(ValidationError):
    """Raised when input validation fails."""
    pass


class ConfigurationError(ValidationError):
    """Raised when configuration is invalid."""
    pass


# Business Logic Exceptions
class BusinessLogicError(GemaibotBaseException):
    """Base class for business logic errors."""
    pass


class UserLimitExceededError(BusinessLogicError):
    """Raised when user limits are exceeded."""
    pass


class DocumentProcessingError(BusinessLogicError):
    """Raised when document processing fails."""
    pass


class ChatStateError(BusinessLogicError):
    """Raised when chat state operations fail."""
    pass


# Cache Exceptions
class CacheError(GemaibotBaseException):
    """Base class for cache-related errors."""
    pass


class RedisConnectionError(CacheError):
    """Raised when Redis connection fails."""
    pass


class CacheKeyError(CacheError):
    """Raised when cache key operations fail."""
    pass


# Security Exceptions
class SecurityError(GemaibotBaseException):
    """Base class for security-related errors."""
    pass


class InputSanitizationError(SecurityError):
    """Raised when input sanitization fails."""
    pass


class AuthenticationError(SecurityError):
    """Raised when authentication fails."""
    pass


# Utility function to convert generic exceptions to typed ones
def convert_to_typed_exception(exception: Exception, context: str = "") -> GemaibotBaseException:
    """Converts generic exceptions to typed exceptions based on context."""
    
    error_message = str(exception)
    error_type = type(exception).__name__
    
    # Database exceptions
    if "asyncpg" in error_type or "postgres" in error_message.lower():
        if "connection" in error_message.lower() or "timeout" in error_message.lower():
            return DatabaseConnectionError(f"Database connection failed: {error_message}", 
                                        {"original_error": error_type, "context": context})
        elif "rate limit" in error_message.lower() or "quota" in error_message.lower():
            return DatabaseRateLimitError(f"Database rate limit exceeded: {error_message}", 
                                        {"original_error": error_type, "context": context})
        else:
            return DatabaseQueryError(f"Database query failed: {error_message}", 
                                   {"original_error": error_type, "context": context})
    
    # Network exceptions
    elif "httpx" in error_type or "connection" in error_message.lower():
        if "timeout" in error_message.lower():
            return ConnectionTimeoutError(f"Connection timeout: {error_message}", 
                                       {"original_error": error_type, "context": context})
        elif "refused" in error_message.lower():
            return ConnectionRefusedError(f"Connection refused: {error_message}", 
                                        {"original_error": error_type, "context": context})
        else:
            return NetworkError(f"Network error: {error_message}", 
                             {"original_error": error_type, "context": context})
    
    # API exceptions
    elif "api" in error_message.lower() or "gemini" in error_message.lower():
        if "quota" in error_message.lower() or "limit" in error_message.lower():
            return APIQuotaExceededError(f"API quota exceeded: {error_message}", 
                                       {"original_error": error_type, "context": context})
        else:
            return APIError(f"API error: {error_message}", 
                          {"original_error": error_type, "context": context})
    
    # Default fallback
    return GemaibotBaseException(f"Unexpected error: {error_message}", 
                               {"original_error": error_type, "context": context})
