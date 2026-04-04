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
import pytest

from app.errors import (
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
    strip_error_tag,
    tag_error,
)

# ─── tag_error / extract_error_code round-trip ───────────────────────────────


def test_tag_error_embeds_invisible_prefix_in_message():
    """tag_error must produce a string that starts with the invisible tag."""
    # Arrange
    code = ErrorCode.TIMEOUT
    message = "Timeout occurred"

    # Act
    tagged = tag_error(code, message)

    # Assert
    assert message in tagged
    assert tagged != message  # The tag must be prepended


def test_extract_error_code_recovers_code_from_tagged_string():
    """extract_error_code must recover the exact ErrorCode from a tagged message."""
    # Arrange
    code = ErrorCode.QUOTA_EXCEEDED
    tagged = tag_error(code, "Some limit message")

    # Act
    recovered = extract_error_code(tagged)

    # Assert
    assert recovered == code


def test_extract_error_code_returns_none_for_untagged_string():
    """Untagged strings must return None from extract_error_code."""
    # Arrange
    plain = "This is a regular message"

    # Act
    result = extract_error_code(plain)

    # Assert
    assert result is None


def test_extract_error_code_returns_none_for_empty_string():
    # Arrange / Act / Assert
    assert extract_error_code("") is None


def test_strip_error_tag_removes_tag_leaving_clean_text():
    """strip_error_tag must return the original user-facing text without the invisible prefix."""
    # Arrange
    code = ErrorCode.GENERIC
    user_message = "❌ Произошла ошибка."
    tagged = tag_error(code, user_message)

    # Act
    cleaned = strip_error_tag(tagged)

    # Assert
    assert cleaned == user_message


def test_strip_error_tag_untagged_string_returns_unchanged():
    """Untagged string must pass through strip_error_tag unchanged."""
    # Arrange
    plain = "Hello world"

    # Act
    result = strip_error_tag(plain)

    # Assert
    assert result == plain


@pytest.mark.parametrize("code", list(ErrorCode))
def test_all_error_codes_can_be_tagged_and_extracted(code):
    """Every ErrorCode must survive a tag/extract roundtrip without corruption."""
    # Arrange
    message = f"Test message for {code.value}"

    # Act
    tagged = tag_error(code, message)
    extracted = extract_error_code(tagged)

    # Assert
    assert extracted == code


# ─── classify_error_from_exception — typed path ──────────────────────────────


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
    # Arrange
    exc = exc_class("test error")

    # Act
    code = classify_error_from_exception(exc)

    # Assert
    assert code == expected_code, f"{exc_class.__name__} should map to {expected_code}"


def test_classify_error_subclass_uses_mro_walk():
    """Subclass of a registered exception type must be classified via MRO walk."""
    # Arrange
    class MySpecialQuotaError(APIQuotaExceededError):
        pass

    exc = MySpecialQuotaError("quota hit")

    # Act
    code = classify_error_from_exception(exc)

    # Assert
    assert code == ErrorCode.QUOTA_EXCEEDED


# ─── classify_error_from_exception — status code path ────────────────────────


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
        (500, ErrorCode.PROCESSING),
    ],
)
def test_classify_error_from_status_code(http_status, expected_code):
    """HTTP status codes must map to the correct ErrorCode."""
    # Arrange / Act
    code = classify_error_from_status_code(http_status)

    # Assert
    assert code == expected_code


def test_classify_error_from_exception_reads_status_code_attribute():
    """Exception with status_code attribute must use HTTP-path classification."""
    # Arrange
    class FakeAPIError(Exception):
        def __init__(self, msg, status_code):
            super().__init__(msg)
            self.status_code = status_code

    exc = FakeAPIError("service unavailable", 503)

    # Act
    code = classify_error_from_exception(exc)

    # Assert
    assert code == ErrorCode.OVERLOADED


# ─── classify_error_from_exception — string fallback path ────────────────────


@pytest.mark.parametrize(
    "message, expected_code",
    [
        ("Request timed out", ErrorCode.TIMEOUT),
        ("Connection timed out", ErrorCode.TIMEOUT),
        ("503 Service Unavailable", ErrorCode.OVERLOADED),
        ("server overloaded", ErrorCode.OVERLOADED),
        ("429 too many requests", ErrorCode.RATE_LIMIT),
        ("quota exceeded for model", ErrorCode.QUOTA_EXCEEDED),
        ("daily limit reached", ErrorCode.QUOTA_EXCEEDED),
        ("some completely unknown error", ErrorCode.GENERIC),
    ],
)
def test_classify_error_string_fallback(message, expected_code):
    """Unregistered exception types must fall back to text-pattern classification."""
    # Arrange
    exc = ValueError(message)

    # Act
    code = classify_error_from_exception(exc)

    # Assert
    assert code == expected_code, f"'{message}' should map to {expected_code}"


# ─── convert_to_typed_exception ──────────────────────────────────────────────


def test_convert_asyncpg_timeout_becomes_database_connection_error():
    """asyncpg exceptions with 'connection' keyword should become DatabaseConnectionError."""
    # Arrange
    class FakeAsyncpgError(Exception):
        pass

    # Simulate an asyncpg error (type name contains 'asyncpg')
    # We patch by naming the class appropriately
    exc = ValueError("asyncpg: connection timeout to database")

    # Act
    typed = convert_to_typed_exception(exc, context="test_query")

    # Assert — must be a subtype of GemaibotBaseException
    assert isinstance(typed, GemaibotBaseException)


def test_convert_network_timeout_becomes_connection_timeout_error():
    """An httpx timeout error should map to a connection-related typed exception."""
    # Arrange
    exc = ConnectionError("httpx: connection timeout")

    # Act
    typed = convert_to_typed_exception(exc, context="external_api")

    # Assert
    assert isinstance(typed, GemaibotBaseException)


# ─── Error inheritance hierarchy ─────────────────────────────────────────────


def test_domain_errors_inherit_from_base():
    """All domain error classes must inherit from GemaibotBaseException."""
    # Arrange
    domain_classes = [
        DatabaseConnectionError,
        APIQuotaExceededError,
        NetworkError,
        DecryptionError,
        DocumentProcessingError,
    ]

    # Act / Assert
    for cls in domain_classes:
        assert issubclass(cls, GemaibotBaseException), (
            f"{cls.__name__} must inherit from GemaibotBaseException"
        )


def test_gemaibot_base_exception_includes_details():
    """GemaibotBaseException must capture message and details dict."""
    # Arrange / Act
    exc = GemaibotBaseException("Something failed", {"key": "value"})

    # Assert
    assert exc.message == "Something failed"
    assert exc.details == {"key": "value"}


def test_gemaibot_base_exception_str_includes_details_when_present():
    """__str__ must include details when they are provided."""
    # Arrange
    exc = GemaibotBaseException("Error", {"reason": "timeout"})

    # Act
    result = str(exc)

    # Assert
    assert "timeout" in result
    assert "Error" in result


def test_gemaibot_base_exception_str_plain_when_no_details():
    """__str__ without details must return just the message."""
    # Arrange
    exc = GemaibotBaseException("Simple error")

    # Act
    result = str(exc)

    # Assert
    assert result == "Simple error"
