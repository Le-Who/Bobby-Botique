"""
Extended AAA unit tests for app.errors — ErrorCode tagging system.

Covers gaps in the existing test_errors.py:
- tag_error / extract_error_code roundtrip
- strip_error_tag
- classify_error_from_exception — type-based O(1) path
- classify_error_from_exception — HTTP status code path
- classify_error_from_exception — string fallback path
- classify_error_from_status_code
- convert_to_typed_exception
- Error inheritance hierarchy

These tests verify behavior (observable transformations) not implementation details.
"""

import asyncpg
import httpx
import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.error import NetworkError as TelegramNetworkError

from app.errors import (
    APIError,
    APIInvalidResponseError,
    APIQuotaExceededError,
    AuthenticationError,
    CircuitBreakerOpenError,
    ConnectionTimeoutError,
    DatabaseConnectionError,
    DatabaseQueryError,
    DatabaseRateLimitError,
    DecryptionError,
    DocumentProcessingError,
    ErrorCode,
    GemaibotBaseException,
    GeminiAPIError,
    NetworkError,
    ServiceConnectionRefusedError,
    TavilyAPIError,
    UserLimitExceededError,
    classify_error_from_exception,
    classify_error_from_status_code,
    convert_to_typed_exception,
    extract_error_code,
    is_retryable_error,
    strip_error_tag,
    tag_error,
    user_friendly_error,
)


def test_tag_error_embeds_invisible_prefix_in_message():
    """tag_error must produce a string that starts with the invisible tag."""
    code = ErrorCode.TIMEOUT
    message = "Timeout occurred"

    tagged = tag_error(code, message)

    assert message in tagged
    assert tagged != message


def test_extract_error_code_recovers_code_from_tagged_string():
    """extract_error_code must recover the exact ErrorCode from a tagged message."""
    code = ErrorCode.QUOTA_EXCEEDED
    tagged = tag_error(code, "Some limit message")

    recovered = extract_error_code(tagged)

    assert recovered == code


def test_extract_error_code_returns_none_for_untagged_string():
    """Untagged strings must return None from extract_error_code."""
    plain = "This is a regular message"

    result = extract_error_code(plain)

    assert result is None


def test_extract_error_code_returns_none_for_empty_string():
    assert extract_error_code("") is None


def test_strip_error_tag_removes_tag_leaving_clean_text():
    """strip_error_tag must return the original user-facing text without the invisible prefix."""
    code = ErrorCode.GENERIC
    user_message = "❌ Произошла ошибка."
    tagged = tag_error(code, user_message)

    cleaned = strip_error_tag(tagged)

    assert cleaned == user_message


def test_strip_error_tag_untagged_string_returns_unchanged():
    """Untagged string must pass through strip_error_tag unchanged."""
    plain = "Hello world"

    result = strip_error_tag(plain)

    assert result == plain


@pytest.mark.parametrize("code", list(ErrorCode))
def test_all_error_codes_can_be_tagged_and_extracted(code):
    """Every ErrorCode must survive a tag/extract roundtrip without corruption."""
    message = f"Test message for {code.value}"

    tagged = tag_error(code, message)
    extracted = extract_error_code(tagged)

    assert extracted == code


@pytest.mark.parametrize(
    "exc_class, expected_code",
    [
        (DatabaseConnectionError, ErrorCode.TIMEOUT),
        (DatabaseRateLimitError, ErrorCode.RATE_LIMIT),
        (DatabaseQueryError, ErrorCode.PROCESSING),
        (ConnectionTimeoutError, ErrorCode.TIMEOUT),
        (ServiceConnectionRefusedError, ErrorCode.NETWORK),
        (CircuitBreakerOpenError, ErrorCode.OVERLOADED),
        (APIQuotaExceededError, ErrorCode.QUOTA_EXCEEDED),
        (APIInvalidResponseError, ErrorCode.INVALID_RESPONSE),
        (GeminiAPIError, ErrorCode.PROCESSING),
        (TavilyAPIError, ErrorCode.NETWORK),
        (NetworkError, ErrorCode.NETWORK),
        (DocumentProcessingError, ErrorCode.DOCUMENT),
        (UserLimitExceededError, ErrorCode.USER_RATE_LIMIT),
        (DecryptionError, ErrorCode.DECRYPTION_FAILED),
        (AuthenticationError, ErrorCode.INVALID_KEY),
    ],
    ids=lambda x: x.value if isinstance(x, ErrorCode) else x.__name__,
)
def test_classify_error_from_exception_typed_path(exc_class, expected_code):
    """Type-based exception classification must resolve O(1) without string scanning."""
    exc = exc_class("test error")

    code = classify_error_from_exception(exc)

    assert code == expected_code, f"{exc_class.__name__} should map to {expected_code}"


def test_classify_error_subclass_uses_mro_walk():
    """Subclass of a registered exception type must be classified via MRO walk."""

    class MySpecialQuotaError(APIQuotaExceededError):
        pass

    exc = MySpecialQuotaError("quota hit")

    code = classify_error_from_exception(exc)

    assert code == ErrorCode.QUOTA_EXCEEDED


@pytest.mark.parametrize("exception_type", [TimeoutError, type("ProviderTimeout", (TimeoutError,), {})])
def test_empty_timeout_exception_retains_timeout_classification(exception_type):
    error = exception_type()

    assert str(error) == ""
    assert classify_error_from_exception(error) == ErrorCode.TIMEOUT
    assert extract_error_code(user_friendly_error(error)) == ErrorCode.TIMEOUT


@pytest.mark.parametrize(
    "error_type, args, expected",
    [
        (httpx.ReadTimeout, ("",), ErrorCode.TIMEOUT),
        (httpx.ConnectError, ("",), ErrorCode.NETWORK),
        (httpx.RemoteProtocolError, ("",), ErrorCode.NETWORK),
        (TimedOut, (), ErrorCode.TIMEOUT),
        (TelegramNetworkError, ("",), ErrorCode.NETWORK),
        (BadRequest, ("synthetic rejected request",), ErrorCode.INVALID_REQUEST),
        (RetryAfter, (17,), ErrorCode.RATE_LIMIT),
    ],
)
def test_transport_errors_classify_without_matching_exception_message(monkeypatch, error_type, args, expected):
    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    error = error_type(*args)
    assert classify_error_from_exception(error) is expected
    assert extract_error_code(user_friendly_error(error)) is expected


@pytest.mark.parametrize(
    "status, expected",
    [
        (400, ErrorCode.INVALID_REQUEST),
        (401, ErrorCode.INVALID_KEY),
        (402, ErrorCode.QUOTA_EXCEEDED),
        (429, ErrorCode.RATE_LIMIT),
    ],
)
def test_httpx_status_uses_response_metadata_without_raw_error_text(status, expected):
    request = httpx.Request("GET", "https://offline.invalid")
    response = httpx.Response(status, request=request)
    error = httpx.HTTPStatusError("synthetic rejection", request=request, response=response)

    assert classify_error_from_exception(error) is expected
    assert extract_error_code(user_friendly_error(error)) is expected


@pytest.mark.parametrize("error", [TimeoutError(), httpx.ReadTimeout("")])
def test_api_error_factory_preserves_typed_retryable_failure(error):
    converted = APIError.from_exception(error)

    assert converted.retryable is True
    assert converted.key_related is False
    assert extract_error_code(converted.user_message) is ErrorCode.TIMEOUT


def test_rate_limit_user_message_retains_retryable_rate_limit_code():
    message = user_friendly_error(ValueError("HTTP 429 rate limit"))

    assert extract_error_code(message) is ErrorCode.RATE_LIMIT
    assert is_retryable_error(message) is True


@pytest.mark.parametrize(
    "http_status, expected_code",
    [
        (429, ErrorCode.RATE_LIMIT),
        (503, ErrorCode.OVERLOADED),
        (502, ErrorCode.NETWORK),
        (504, ErrorCode.TIMEOUT),
        (401, ErrorCode.INVALID_KEY),
        (403, ErrorCode.INVALID_KEY),
        (400, ErrorCode.INVALID_REQUEST),
        (500, ErrorCode.OVERLOADED),
    ],
)
def test_classify_error_from_status_code(http_status, expected_code):
    """HTTP status codes must map to the correct ErrorCode."""
    code = classify_error_from_status_code(http_status)

    assert code == expected_code


def test_classify_error_from_exception_reads_status_code_attribute():
    """Exception with status_code attribute must use HTTP-path classification."""

    class FakeAPIError(Exception):
        def __init__(self, msg, status_code):
            super().__init__(msg)
            self.status_code = status_code

    exc = FakeAPIError("service unavailable", 503)

    code = classify_error_from_exception(exc)

    assert code == ErrorCode.OVERLOADED


@pytest.mark.parametrize(
    "message, expected_code",
    [
        ("Request timed out", ErrorCode.TIMEOUT),
        ("Connection timed out", ErrorCode.TIMEOUT),
        ("503 Service Unavailable", ErrorCode.OVERLOADED),
        ("server overloaded", ErrorCode.OVERLOADED),
        ("429 too many requests", ErrorCode.RATE_LIMIT),
        ("quota exceeded for model", ErrorCode.RATE_LIMIT),
        ("daily quota exceeded for model", ErrorCode.QUOTA_EXCEEDED),
        ("daily limit reached", ErrorCode.QUOTA_EXCEEDED),
        ("some completely unknown error", ErrorCode.GENERIC),
    ],
)
def test_classify_error_string_fallback(message, expected_code):
    """Unregistered exception types must fall back to text-pattern classification."""
    exc = ValueError(message)

    code = classify_error_from_exception(exc)

    assert code == expected_code, f"'{message}' should map to {expected_code}"


def test_convert_asyncpg_connection_loss_becomes_database_connection_error():
    """asyncpg exceptions with 'connection' keyword should become DatabaseConnectionError."""

    exc = asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation")

    typed = convert_to_typed_exception(exc, context="test_query")

    assert type(typed) is DatabaseConnectionError
    assert typed.details == {"original_error": "ConnectionDoesNotExistError", "context": "test_query"}


def test_convert_network_timeout_becomes_connection_timeout_error():
    """An httpx timeout error should map to a connection-related typed exception."""
    exc = httpx.ConnectTimeout("connection timeout", request=httpx.Request("GET", "https://example.test"))

    typed = convert_to_typed_exception(exc, context="external_api")

    assert type(typed) is ConnectionTimeoutError
    assert typed.details == {"original_error": "ConnectTimeout", "context": "external_api"}


def test_domain_errors_inherit_from_base():
    """All domain error classes must inherit from GemaibotBaseException."""
    domain_classes = [
        DatabaseConnectionError,
        APIQuotaExceededError,
        NetworkError,
        DecryptionError,
        DocumentProcessingError,
    ]

    for cls in domain_classes:
        assert issubclass(cls, GemaibotBaseException), f"{cls.__name__} must inherit from GemaibotBaseException"


def test_gemaibot_base_exception_includes_details():
    """GemaibotBaseException must capture message and details dict."""
    exc = GemaibotBaseException("Something failed", {"key": "value"})

    assert exc.message == "Something failed"
    assert exc.details == {"key": "value"}


def test_gemaibot_base_exception_str_includes_details_when_present():
    """__str__ must include details when they are provided."""
    exc = GemaibotBaseException("Error", {"reason": "timeout"})

    result = str(exc)

    assert "timeout" in result
    assert "Error" in result


def test_gemaibot_base_exception_str_plain_when_no_details():
    """__str__ without details must return just the message."""
    exc = GemaibotBaseException("Simple error")

    result = str(exc)

    assert result == "Simple error"
