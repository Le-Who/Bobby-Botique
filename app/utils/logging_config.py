"""
Centralized logging configuration for GemAI Bot using structlog.

Provides:
- setup_detailed_logging() for initial configuration
- get_logger() helper for module-specific loggers
- timed_operation() for async performance tracking
- Bridge for legacy RequestContextvars to structlog formatting
"""

import asyncio
import atexit
import functools
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import TextIO

try:
    import structlog
    from structlog.typing import Processor

    HAS_STRUCTLOG = True
except ImportError:
    HAS_STRUCTLOG = False

from app.observability.config import LoggingSettings
from app.observability.events import emit, record_exception
from app.observability.writer import BoundedLogWriter, BoundedQueueHandler
from app.request_context import get_chat_id, get_request_id, get_user_id

_LOGGING_LIFECYCLE_LOCK = threading.RLock()
_ACTIVE_WRITER: BoundedLogWriter | None = None
_ACTIVE_HANDLER: BoundedQueueHandler | None = None
_OWNED_STREAMS: list[TextIO] = []
_ATEXIT_REGISTERED = False
_HOOKS_INSTALLED = False
_PREVIOUS_SYS_EXCEPTHOOK = sys.excepthook
_PREVIOUS_THREAD_EXCEPTHOOK = threading.excepthook

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


def configure_structlog_pipeline(
    enable_structured: bool,
    enable_pretty: bool,
    event_max_bytes: int = 32_768,
) -> logging.Formatter:
    """Configures structlog to intercept and format log records."""
    from app.observability.pipeline import enforce_event_budget, normalize_event, remove_processor_meta

    callsite_parameters = {
        structlog.processors.CallsiteParameter.PATHNAME,
        structlog.processors.CallsiteParameter.FUNC_NAME,
        structlog.processors.CallsiteParameter.LINENO,
    }
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.stdlib.ExtraAdder(),
        structlog.processors.CallsiteParameterAdder(callsite_parameters),
        normalize_event,
        enforce_event_budget(event_max_bytes),
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],  # type: ignore
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if enable_structured
        else structlog.dev.ConsoleRenderer(colors=enable_pretty)
    )
    return structlog.stdlib.ProcessorFormatter(
        processors=[remove_processor_meta, renderer],
        foreign_pre_chain=shared_processors,
    )


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


def _setup_logger(logger_name: str, level: int) -> None:
    """Route a named logger through the single root writer at its own level."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    for handler in logger.handlers[:]:
        if getattr(handler, "_gemaibot_owned", False):
            logger.removeHandler(handler)
            handler.close()
    logger.propagate = True


class _FanoutStream:
    """Write each serialized line to all configured sinks from the writer thread."""

    def __init__(
        self,
        streams: Sequence[TextIO],
        *,
        failure_event_factory: Callable[[str, str, int], str] | None = None,
    ) -> None:
        self._streams = tuple(streams)
        self._failure_event_factory = failure_event_factory
        self._last_failure_report: dict[tuple[str, int], float] = {}

    def _report_partial_failure(
        self,
        phase: str,
        failures: list[tuple[int, Exception]],
        surviving_streams: list[TextIO],
    ) -> None:
        if self._failure_event_factory is None:
            return
        now = time.monotonic()
        for sink_index, error in failures:
            key = (phase, sink_index)
            if now - self._last_failure_report.get(key, 0.0) < 30.0:
                continue
            self._last_failure_report[key] = now
            try:
                diagnostic = self._failure_event_factory(phase, type(error).__name__, sink_index)
            except Exception:
                continue
            for stream in surviving_streams:
                try:
                    stream.write(diagnostic)
                    stream.flush()
                except Exception:
                    pass

    def write(self, value: str) -> int:
        first_error: Exception | None = None
        failures: list[tuple[int, Exception]] = []
        surviving_streams: list[TextIO] = []
        for sink_index, stream in enumerate(self._streams):
            try:
                stream.write(value)
                surviving_streams.append(stream)
            except Exception as error:
                first_error = first_error or error
                failures.append((sink_index, error))
        if not surviving_streams and first_error is not None:
            raise first_error
        if failures:
            self._report_partial_failure("write", failures, surviving_streams)
        return len(value)

    def flush(self) -> None:
        first_error: Exception | None = None
        failures: list[tuple[int, Exception]] = []
        surviving_streams: list[TextIO] = []
        for sink_index, stream in enumerate(self._streams):
            try:
                stream.flush()
                surviving_streams.append(stream)
            except Exception as error:
                first_error = first_error or error
                failures.append((sink_index, error))
        if not surviving_streams and first_error is not None:
            raise first_error
        if failures:
            self._report_partial_failure("flush", failures, surviving_streams)


def shutdown_detailed_logging(*, timeout: float = 3.0) -> bool:
    """Stop the owned writer once and drain already accepted events."""
    global _ACTIVE_HANDLER, _ACTIVE_WRITER, _HOOKS_INSTALLED, _OWNED_STREAMS

    with _LOGGING_LIFECYCLE_LOCK:
        handler = _ACTIVE_HANDLER
        writer = _ACTIVE_WRITER
        streams = _OWNED_STREAMS
        _ACTIVE_HANDLER = None
        _ACTIVE_WRITER = None
        _OWNED_STREAMS = []

        if _HOOKS_INSTALLED:
            sys.excepthook = _PREVIOUS_SYS_EXCEPTHOOK
            threading.excepthook = _PREVIOUS_THREAD_EXCEPTHOOK
            logging.captureWarnings(False)
            _HOOKS_INSTALLED = False

        if handler is not None:
            root_logger = logging.getLogger()
            if handler in root_logger.handlers:
                root_logger.removeHandler(handler)
            handler.close()

        drained = True if writer is None else writer.stop(timeout=timeout)
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
        return drained


def _install_exception_hooks() -> None:
    global _HOOKS_INSTALLED

    def process_exception_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback_value: TracebackType | None,
    ) -> None:
        if issubclass(exception_type, (KeyboardInterrupt, SystemExit)):
            _PREVIOUS_SYS_EXCEPTHOOK(exception_type, exception, traceback_value)
            return
        record_exception(
            "process.uncaught_exception",
            exception,
            operation="process.lifecycle",
            level="critical",
        )

    def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            return
        if args.exc_value is not None:
            record_exception(
                "thread.uncaught_exception",
                args.exc_value,
                operation="thread.lifecycle",
                level="critical",
                fields={"thread_name": args.thread.name if args.thread else None},
            )

    sys.excepthook = process_exception_hook
    threading.excepthook = thread_exception_hook
    logging.captureWarnings(True)
    _HOOKS_INSTALLED = True


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Route unhandled task/transport failures through the same event envelope."""
    target = loop or asyncio.get_running_loop()

    def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        error = context.get("exception")
        if isinstance(error, asyncio.CancelledError):
            return
        fields = {
            "_event_name": "asyncio.unhandled_exception",
            "operation": "asyncio.lifecycle",
            "asyncio_message": str(context.get("message") or "Unhandled asyncio exception"),
            "future_type": type(context.get("future") or context.get("task")).__name__,
        }
        if isinstance(error, BaseException):
            record_exception(
                "asyncio.unhandled_exception",
                error,
                operation="asyncio.lifecycle",
                fields={
                    "asyncio_message": fields["asyncio_message"],
                    "future_type": fields["future_type"],
                },
            )
        else:
            emit(
                "asyncio.unhandled_exception",
                level="error",
                operation="asyncio.lifecycle",
                asyncio_message=fields["asyncio_message"],
                future_type=fields["future_type"],
            )

    target.set_exception_handler(handler)


def setup_detailed_logging(
    log_level: str | None = None,
    log_to_file: bool = False,
    log_file_path: str = "/tmp/bot_detailed.log",
    enable_structured_logging: bool | None = None,
    enable_pretty: bool | None = None,
) -> None:
    """Configure logging for all bot components and setup structlog bridging."""

    global _ACTIVE_HANDLER, _ACTIVE_WRITER, _ATEXIT_REGISTERED, _OWNED_STREAMS

    # Register bootstrap credentials before any application event can echo them.
    # The exact value is retained only in the in-process scrubber set.
    from app.observability.redaction import register_sensitive_credential

    register_sensitive_credential("bot_token", os.environ.get("TELEGRAM_BOT_TOKEN"))

    resolved = LoggingSettings.from_environ()
    log_level = log_level or resolved.level_name

    # Explicit function options remain useful for tests and embedders. Runtime
    # startup calls without overrides and therefore uses the canonical resolver.
    if enable_structured_logging is None:
        enable_structured_logging = resolved.format == "json"

    if enable_pretty is None:
        enable_pretty = os.environ.get("LOG_PRETTY", "").lower() in ("1", "true", "yes")

    if enable_structured_logging:
        enable_pretty = False

    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    if HAS_STRUCTLOG:
        formatter = configure_structlog_pipeline(
            enable_structured_logging,
            enable_pretty,
            resolved.event_max_bytes,
        )
    else:
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    with _LOGGING_LIFECYCLE_LOCK:
        shutdown_detailed_logging()
        root_logger = logging.getLogger()
        root_logger.setLevel(numeric_level)
        for handler in root_logger.handlers[:]:
            if getattr(handler, "_gemaibot_owned", False):
                root_logger.removeHandler(handler)
                handler.close()

        streams: list[TextIO] = [sys.stdout]
        owned_streams: list[TextIO] = []
        file_open_error: Exception | None = None
        if log_to_file:
            try:
                file_stream = open(log_file_path, "a", encoding="utf-8")  # noqa: SIM115
                streams.append(file_stream)
                owned_streams.append(file_stream)
            except Exception as error:
                file_open_error = error

        max_events = resolved.queue_max_events
        max_bytes = resolved.queue_max_bytes
        from app.observability.pipeline import standalone_event_bytes

        writer = BoundedLogWriter(
            _FanoutStream(
                streams,
                failure_event_factory=lambda phase, error_type, sink_index: (
                    standalone_event_bytes(
                        "logging.sink_failed",
                        level="error",
                        message="A configured logging sink failed; surviving sinks remain active",
                        fields={
                            "operation": "logging.write",
                            "failure_phase": phase,
                            "error_type": error_type,
                            "sink_index": sink_index,
                        },
                    ).decode("utf-8")
                    + "\n"
                ),
            ),
            max_events=max_events,
            max_bytes=max_bytes,
            loss_summary_factory=lambda snapshot: standalone_event_bytes(
                "logging.loss_summary",
                level="warning",
                message="Log events were dropped by the bounded writer",
                fields=snapshot,
            ),
            recovery_summary_factory=lambda snapshot: standalone_event_bytes(
                "logging.sink_recovered",
                level="warning",
                message="Logging output recovered after one or more uncertain event deliveries",
                fields=snapshot,
            ),
        )
        handler = BoundedQueueHandler(writer, numeric_level)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        _ACTIVE_WRITER = writer
        _ACTIVE_HANDLER = handler
        _OWNED_STREAMS = owned_streams
        _install_exception_hooks()

        # Suppress loud HTTP client internals while retaining one shared sink.
        for logger_name in ["api_logger", "telegram", "asyncpg", "httpx", "httpcore"]:
            _setup_logger(
                logger_name,
                max(numeric_level, logging.WARNING) if logger_name in ("httpx", "httpcore") else numeric_level,
            )

        if not _ATEXIT_REGISTERED:
            atexit.register(shutdown_detailed_logging)
            _ATEXIT_REGISTERED = True

        if file_open_error is not None:
            logging.warning(
                "Logging file sink could not be opened",
                extra={
                    "_event_name": "logging.file_sink_unavailable",
                    "path": log_file_path,
                    "error_type": type(file_open_error).__name__,
                },
            )
        if resolved.invalid_parameters:
            logging.warning(
                "Invalid logging queue limit; safe default applied",
                extra={
                    "_event_name": "logging.invalid_configuration",
                    "invalid_parameters": list(resolved.invalid_parameters),
                },
            )
        if resolved.conflicts:
            logging.warning(
                "Canonical logging configuration overrides compatibility aliases",
                extra={
                    "_event_name": "logging.configuration_conflict",
                    "conflicting_parameters": list(resolved.conflicts),
                },
            )

        from app.observability.diagnostics import announce_diagnostic_state

        announce_diagnostic_state(resolved)

        logging.info(
            "Logging setup complete — level=%s, structlog=%s, structured=%s, pretty=%s",
            log_level,
            HAS_STRUCTLOG,
            enable_structured_logging,
            enable_pretty,
            extra={
                "_event_name": "logging.configured",
                "queue_max_events": max_events,
                "queue_max_bytes": max_bytes,
            },
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
