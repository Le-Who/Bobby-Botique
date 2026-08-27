"""
Centralized logging configuration for GemAI Bot using structlog.

Provides:
- setup_detailed_logging() for initial configuration
- get_logger() helper for module-specific loggers
- timed_operation() for async performance tracking
- Bridge for legacy RequestContextvars to structlog formatting
"""

import functools
import logging
import os
import sys
import time

try:
    import structlog

    HAS_STRUCTLOG = True
except ImportError:
    HAS_STRUCTLOG = False

from app.request_context import get_chat_id, get_request_id, get_user_id

# =============================================================================
# STRUCTLOG PIPELINE
# =============================================================================


def bridge_legacy_contextvars(logger, method_name, event_dict):
    """Bridge legacy thread-local context variables into structlog."""
    if req_id := get_request_id():
        event_dict["request_id"] = req_id
    if user_id := get_user_id():
        event_dict["user_id"] = user_id
    if chat_id := get_chat_id():
        event_dict["chat_id"] = chat_id
    return event_dict


def configure_structlog_pipeline(enable_structured: bool, enable_pretty: bool) -> logging.Formatter:
    """Configures structlog to intercept and format log records."""
    import socket

    hostname = os.environ.get("HOSTNAME", socket.gethostname())
    service = os.environ.get("SERVICE_NAME", "gemaibotv2")

    def add_service_vars(logger, method_name, event_dict):
        event_dict["service"] = service
        event_dict["hostname"] = hostname
        return event_dict

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        bridge_legacy_contextvars,
        add_service_vars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso" if enable_structured else "%Y-%m-%d %H:%M:%S"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],  # type: ignore
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    if enable_structured:
        return structlog.stdlib.ProcessorFormatter(processor=structlog.processors.JSONRenderer())
    return structlog.stdlib.ProcessorFormatter(processor=structlog.dev.ConsoleRenderer(colors=enable_pretty))


# =============================================================================
# REQUEST CONTEXT FILTER
# =============================================================================


class RequestContextFilter(logging.Filter):
    """Logging filter that injects request/user context into every log record.

    Reads from the asyncio context-var store set by ``set_request_id`` /
    ``set_user_context`` and stamps each record with:
      - ``record.request_id``
      - ``record.user_id``
      - ``record.chat_id``

    Attach to any ``logging.Handler`` to get automatic correlation IDs in all
    log lines without changing call-sites.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.request_id = get_request_id()
        record.user_id = get_user_id()
        record.chat_id = get_chat_id()
        return True


# Fallback basic formatter if structlog is missing
class FallbackFormatter(logging.Formatter):
    def format(self, record):
        return super().format(record)


# =============================================================================
# SETUP FUNCTIONS
# =============================================================================


def _is_production_environment() -> bool:
    """Detect if we're running in a production container environment."""
    indicators = ("DYNO", "RENDER", "RAILWAY_ENVIRONMENT", "FLY_APP_NAME")
    if any(os.environ.get(k) for k in indicators):
        return True
    hostname = os.environ.get("HOSTNAME", "")
    return bool(os.environ.get("PORT") and len(hostname) >= 12 and hostname.isalnum())


def _setup_logger(logger_name: str, level: int, formatter: logging.Formatter) -> None:
    """Configure a named logger with standard settings."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.propagate = False


def setup_detailed_logging(
    log_level: str = "INFO",
    log_to_file: bool = False,
    log_file_path: str = "/tmp/bot_detailed.log",
    enable_structured_logging: bool | None = None,
    enable_pretty: bool | None = None,
) -> None:
    """Configure logging for all bot components and setup structlog bridging."""

    # Auto-resolve settings
    if enable_structured_logging is None:
        env_val = os.environ.get("STRUCTURED_LOGGING", "").lower()
        if env_val in ("1", "true", "yes"):
            enable_structured_logging = True
        elif env_val in ("0", "false", "no"):
            enable_structured_logging = False
        else:
            enable_structured_logging = _is_production_environment()

    if enable_pretty is None:
        enable_pretty = os.environ.get("LOG_PRETTY", "").lower() in ("1", "true", "yes")

    if enable_structured_logging:
        enable_pretty = False

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    if HAS_STRUCTLOG:
        formatter = configure_structlog_pipeline(enable_structured_logging, enable_pretty)
    else:
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    if log_to_file:
        try:
            file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
            file_handler.setLevel(numeric_level)
            # File always plainly formatted
            if HAS_STRUCTLOG:
                file_formatter = configure_structlog_pipeline(enable_structured_logging, False)
            else:
                file_formatter = formatter
            file_handler.setFormatter(file_formatter)
            root_logger.addHandler(file_handler)
        except Exception:
            pass

    # Suppress loud HTTPX logging
    for logger_name in ["api_logger", "telegram", "asyncpg", "httpx", "httpcore"]:
        _setup_logger(
            logger_name,
            max(numeric_level, logging.WARNING) if logger_name in ("httpx", "httpcore") else numeric_level,
            formatter,
        )

    logging.info(
        "Logging setup complete — level=%s, structlog=%s, structured=%s, pretty=%s",
        log_level,
        HAS_STRUCTLOG,
        enable_structured_logging,
        enable_pretty,
    )


# =============================================================================
# LOGGER HELPERS
# =============================================================================


def get_logger(name: str):
    """
    Get a structlog-aware logger config.
    Drop-in compatibility with: logger = get_logger(__name__)
    """
    if HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)


def timed_operation(operation_name: str = ""):
    """Decorator that logs the execution time of async functions."""

    def decorator(fn):
        name = operation_name or fn.__qualname__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            log = get_logger(__name__)
            try:
                result = await fn(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - start) * 1000
                if elapsed_ms > 500:
                    log.warning(
                        f"Slow operation {name}: {elapsed_ms:.1f}ms", operation=name, duration_ms=round(elapsed_ms, 1)
                    )
                else:
                    log.debug(f"Operation {name}: {elapsed_ms:.1f}ms", operation=name, duration_ms=round(elapsed_ms, 1))
                return result
            except Exception:
                elapsed_ms = (time.perf_counter() - start) * 1000
                log.debug(
                    f"Operation {name} failed after {elapsed_ms:.1f}ms",
                    operation=name,
                    duration_ms=round(elapsed_ms, 1),
                )
                raise

        return wrapper

    return decorator


# Legacy compatibility functions
def setup_api_logger(level: int, enable_structured_logging: bool = False) -> None:
    pass


def setup_telegram_logger(level: int, enable_structured_logging: bool = False) -> None:
    pass


def setup_database_logger(level: int, enable_structured_logging: bool = False) -> None:
    pass


def is_pretty_logging() -> bool:
    return os.environ.get("LOG_PRETTY", "").lower() in ("1", "true", "yes")


def log_api_summary() -> None:
    logging.info("API logging active — Gemini, Tavily, Telegram request/response + error tracing")
