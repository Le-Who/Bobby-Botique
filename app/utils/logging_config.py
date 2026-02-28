"""
Centralized logging configuration for GemAI Bot.

Provides:
- setup_detailed_logging() for initial configuration
- JSONFormatter for structured production logging
- DevFormatter for human-readable development logging
- Optional Rich handler for colored, pretty console output
- get_logger() helper for module-specific loggers
- log_with_context() for adding user/chat context to logs
"""

import functools
import json
import logging
import os
import sys
import time
from typing import Any

from app.request_context import get_request_id

# Try importing Rich for pretty dev output (optional dependency)
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.traceback import install as install_rich_tracebacks

    HAS_RICH = True
except ImportError:
    HAS_RICH = False


# =============================================================================
# FORMATTERS
# =============================================================================


class JSONFormatter(logging.Formatter):
    """
    JSON formatter for structured logging in production.

    Automatically includes:
    - timestamp, level, logger, message
    - module, function, line number
    - service, hostname for log aggregation
    - user_id, chat_id if set on record
    - exception info if present
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import socket
        self._hostname = os.environ.get("HOSTNAME", socket.gethostname())
        self._service = os.environ.get("SERVICE_NAME", "gemaibotv2")

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "service": self._service,
            "hostname": self._hostname,
        }

        # Add context fields if present
        if hasattr(record, "user_id"):
            log_entry["user_id"] = record.user_id
        if hasattr(record, "chat_id"):
            log_entry["chat_id"] = record.chat_id
        if hasattr(record, "extra_context"):
            log_entry["context"] = record.extra_context
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id

        # Add exception info
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False)


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        request_id = get_request_id()
        if not hasattr(record, "request_id"):
            record.request_id = request_id or "-"
        return True


# Default formatter for development (compact single-line)
DEFAULT_FORMATTER = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - [request_id=%(request_id)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class DevFormatter(logging.Formatter):
    """
    Human-readable multi-line formatter for local debugging.

    Output example:
        00:57:13 INFO     │ app.handlers.ai_chat     │ handle_message:42
                          │ 🤖 GEMINI REQUEST STARTED
                          │   model: gemini-2.5-flash
                          │   user_id: 123456
    """

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"
    DIM = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp (short)
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))

        # Colorize level
        color = self.COLORS.get(record.levelname, "")
        level = f"{color}{record.levelname:<8}{self.RESET}"

        # Logger name (truncated)
        name = record.name[-25:] if len(record.name) > 25 else record.name

        # Location
        loc = f"{record.funcName}:{record.lineno}"

        # Header line
        header = f"{self.DIM}{ts}{self.RESET} {level} {self.DIM}│{self.RESET} {name:<25} {self.DIM}│{self.RESET} {loc}"

        # Message — try to detect and pretty-print embedded JSON
        msg = record.getMessage()
        body_lines = self._format_message(msg)

        pad = " " * 18  # align with header
        sep = f"{self.DIM}│{self.RESET}"
        body = "\n".join(f"{pad} {sep} {line}" for line in body_lines)

        result = f"{header}\n{body}"

        # Exception info
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            result += f"\n{pad} {sep} {self.COLORS['ERROR']}TRACEBACK:{self.RESET}\n"
            for tb_line in record.exc_text.splitlines():
                result += f"{pad} {sep}   {tb_line}\n"

        return result

    def _format_message(self, msg: str) -> list[str]:
        """Try to extract and pretty-print JSON from the message."""
        # Check if message contains JSON blob (common pattern: "emoji TEXT: {...}")
        json_start = msg.find("{")
        if json_start > 0:
            prefix = msg[:json_start].strip()
            json_part = msg[json_start:]
            try:
                data = json.loads(json_part)
                lines = [prefix]
                for k, v in data.items():
                    if v is not None:
                        lines.append(f"  {self.DIM}{k}:{self.RESET} {v}")
                return lines
            except (json.JSONDecodeError, ValueError):
                pass
        return [msg]


# =============================================================================
# LOGGER HELPERS
# =============================================================================


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger by name with standard configuration.

    Usage:
        logger = get_logger(__name__)
        logger.info("Processing request", extra={"user_id": 123})
    """
    return logging.getLogger(name)


def log_with_context(
    logger: logging.Logger,
    level: int,
    message: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    **extra: Any,
) -> None:
    """
    Log a message with user/chat context attached.

    Args:
        logger: Logger instance
        level: Logging level (logging.INFO, logging.ERROR, etc.)
        message: Log message
        user_id: Optional user ID to attach
        chat_id: Optional chat ID to attach
        **extra: Additional context to include

    Usage:
        log_with_context(logger, logging.INFO, "User action", user_id=123)
    """
    extra_dict = {}
    if user_id is not None:
        extra_dict["user_id"] = user_id
    if chat_id is not None:
        extra_dict["chat_id"] = chat_id
    if extra:
        extra_dict["extra_context"] = extra

    logger.log(level, message, extra=extra_dict)


def timed_operation(operation_name: str = ""):
    """Decorator that logs the execution time of async functions.

    Usage:
        @timed_operation("database_query")
        async def get_user(user_id: int):
            ...
    """
    def decorator(fn):
        name = operation_name or fn.__qualname__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - start) * 1000
                if elapsed_ms > 500:
                    logging.warning(
                        f"Slow operation {name}: {elapsed_ms:.1f}ms",
                        extra={"operation": name, "duration_ms": round(elapsed_ms, 1)},
                    )
                else:
                    logging.debug(
                        f"Operation {name}: {elapsed_ms:.1f}ms",
                        extra={"operation": name, "duration_ms": round(elapsed_ms, 1)},
                    )
                return result
            except Exception:
                elapsed_ms = (time.perf_counter() - start) * 1000
                logging.debug(
                    f"Operation {name} failed after {elapsed_ms:.1f}ms",
                    extra={"operation": name, "duration_ms": round(elapsed_ms, 1)},
                )
                raise

        return wrapper
    return decorator


# =============================================================================
# SETUP FUNCTIONS
# =============================================================================


def _get_formatter(
    enable_structured_logging: bool, enable_pretty: bool = False
) -> logging.Formatter:
    """Get appropriate formatter based on configuration."""
    if enable_structured_logging:
        return JSONFormatter()
    if enable_pretty:
        return DevFormatter()
    return DEFAULT_FORMATTER


def _setup_logger(
    logger_name: str,
    level: int,
    enable_structured_logging: bool,
    enable_pretty: bool = False,
) -> None:
    """
    Configure a named logger with standard settings.

    Args:
        logger_name: Name of the logger to configure
        level: Logging level
        enable_structured_logging: Whether to use JSON format
        enable_pretty: Whether to use human-readable dev format
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    # Don't add handlers if they already exist
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(
            _get_formatter(enable_structured_logging, enable_pretty)
        )
        logger.addHandler(handler)

    # Disable propagation to avoid duplicate logs
    logger.propagate = False


def setup_detailed_logging(
    log_level: str = "INFO",
    log_to_file: bool = False,
    log_file_path: str = "/tmp/bot_detailed.log",
    enable_structured_logging: bool = False,
    enable_pretty: bool | None = None,
) -> None:
    """
    Настраивает детальное логирование for всех компонентов бота.

    Args:
        log_level: Уровень логирования (INFO, WARNING, ERROR, CRITICAL)
        log_to_file: Логировать ли в file
        log_file_path: Путь к fileу логов
        enable_structured_logging: Вkeysть JSON логирование for production
        enable_pretty: Enable human-readable dev logging. Auto-detects
                       from LOG_PRETTY env var when None.
    """
    # Resolve pretty mode from env if not explicitly set
    if enable_pretty is None:
        enable_pretty = os.environ.get("LOG_PRETTY", "").lower() in (
            "1",
            "true",
            "yes",
        )

    # Structured logging takes priority over pretty
    if enable_structured_logging:
        enable_pretty = False

    # Convert string to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Choose handler: Rich > DevFormatter > DEFAULT_FORMATTER
    if enable_pretty and HAS_RICH and not enable_structured_logging:
        # Rich provides its own formatting — no need for our formatter
        install_rich_tracebacks(show_locals=False, width=200)
        stdout_handler = RichHandler(
            console=Console(stderr=False, width=200),
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            show_path=True,
            markup=True,
        )
        stdout_handler.setLevel(numeric_level)
        stdout_handler.addFilter(RequestContextFilter())
        root_logger.addHandler(stdout_handler)
    else:
        formatter = _get_formatter(enable_structured_logging, enable_pretty)
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(numeric_level)
        stdout_handler.addFilter(RequestContextFilter())
        stdout_handler.setFormatter(formatter)
        root_logger.addHandler(stdout_handler)

    # File handler (optional)
    if log_to_file:
        try:
            file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
            file_handler.setLevel(numeric_level)
            file_handler.addFilter(RequestContextFilter())
            # File always uses plain formatter (no ANSI colors)
            file_handler.setFormatter(
                _get_formatter(enable_structured_logging, enable_pretty=False)
            )
            root_logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Could not create file handler: {e}", flush=True)

    # Configure specialized loggers
    # httpx is suppressed to WARNING+ to prevent Bearer tokens from request headers
    # leaking into debug-level logs (H5 security fix).
    for logger_name in ["api_logger", "telegram", "asyncpg", "httpx", "httpcore"]:
        _setup_logger(
            logger_name, max(numeric_level, logging.WARNING) if logger_name in ("httpx", "httpcore") else numeric_level,
            enable_structured_logging, enable_pretty
        )

    pretty_mode = "rich" if (enable_pretty and HAS_RICH) else (
        "dev" if enable_pretty else "off"
    )
    logging.info(
        "Logging setup complete — level=%s, structured=%s, pretty=%s, file=%s",
        log_level,
        enable_structured_logging,
        pretty_mode,
        log_file_path if log_to_file else "disabled",
    )


# Legacy compatibility functions
def setup_api_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для API запросов"""
    _setup_logger("api_logger", level, enable_structured_logging)


def setup_telegram_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для Telegram Bot API"""
    _setup_logger("telegram", level, enable_structured_logging)


def setup_database_logger(level: int, enable_structured_logging: bool = False) -> None:
    """Настраивает логгер для базы данных"""
    _setup_logger("asyncpg", level, enable_structured_logging)


def is_pretty_logging() -> bool:
    """Check if pretty logging is active (for use by api_logger etc.)."""
    return os.environ.get("LOG_PRETTY", "").lower() in ("1", "true", "yes")


def log_api_summary() -> None:
    """Выводит краткую сводку по API логированию."""
    logging.info(
        "API logging active — Gemini, Tavily, Telegram request/response + error tracing"
    )
