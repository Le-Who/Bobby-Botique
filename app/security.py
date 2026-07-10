"""
Security utilities for input sanitization and validation.
Provides protection against malicious input and ensures data safety.
"""

import asyncio
import html
import ipaddress
import logging
import re
import socket
import threading
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from app.errors import InputSanitizationError


class InputSanitizer:
    """Input sanitization and validation utilities."""

    # Dangerous patterns
    DANGEROUS_PATTERNS = [
        r"<script[^>]*>.*?</script>",  # Script tags
        r"<iframe[^>]*>.*?</iframe>",  # Iframe tags
        r"javascript:",  # JavaScript protocol
        r"vbscript:",  # VBScript protocol
        r"data:",  # Data protocol
        r"<[^>]*>",  # HTML tags
        r"&[#\w]+;",  # HTML entities
        r"\\[xX][0-9a-fA-F]{2}",  # Hex escapes
        r"\\[0-7]{1,3}",  # Octal escapes
        r"\\[abfnrtv]",  # Control character escapes
    ]

    # Allowed file extensions
    ALLOWED_EXTENSIONS = {
        "image": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"},
        "document": {".pdf", ".doc", ".docx", ".txt", ".rtf"},
        "archive": {".zip", ".rar", ".7z", ".tar", ".gz"},
    }

    # Maximum lengths
    MAX_LENGTHS = {"message": 4096, "filename": 255, "url": 2048, "query": 1000}

    def __init__(self):
        self.compiled_patterns = [re.compile(pattern, re.IGNORECASE | re.DOTALL) for pattern in self.DANGEROUS_PATTERNS]

    def sanitize_text(self, text: str, max_length: int | None = None) -> str:
        """
        Sanitizes text input by removing dangerous patterns.

        Args:
            text: Input text to sanitize
            max_length: Maximum allowed length

        Returns:
            Sanitized text

        Raises:
            InputSanitizationError: If input is too long or contains dangerous content
        """
        if not isinstance(text, str):
            raise InputSanitizationError(f"Expected string, got {type(text).__name__}")

        # Check length
        max_len = max_length or self.MAX_LENGTHS["message"]
        if len(text) > max_len:
            raise InputSanitizationError(f"Text too long: {len(text)} > {max_len}")

        # Remove dangerous patterns
        sanitized = text
        for pattern in self.compiled_patterns:
            sanitized = pattern.sub("", sanitized)

        # HTML escape remaining content
        sanitized = html.escape(sanitized)

        # Remove multiple spaces and normalize
        sanitized = re.sub(r"\s+", " ", sanitized).strip()

        if not sanitized:
            raise InputSanitizationError("Text contains only dangerous content")

        return sanitized

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitizes filename by removing dangerous characters and extensions.

        Args:
            filename: Input filename

        Returns:
            Sanitized filename

        Raises:
            InputSanitizationError: If filename is invalid
        """
        if not isinstance(filename, str):
            raise InputSanitizationError(f"Expected string, got {type(filename).__name__}")

        # Check length
        if len(filename) > self.MAX_LENGTHS["filename"]:
            raise InputSanitizationError(f"Filename too long: {len(filename)} > {self.MAX_LENGTHS['filename']}")

        # Remove path separators and dangerous characters
        dangerous_chars = r'[<>:"/\\|?*\x00-\x1f]'
        sanitized = re.sub(dangerous_chars, "_", filename)

        # Remove multiple underscores
        sanitized = re.sub(r"_+", "_", sanitized)

        # Remove leading/trailing underscores and dots
        sanitized = sanitized.strip("_.")

        if not sanitized:
            raise InputSanitizationError("Filename contains only dangerous characters")

        return sanitized

    def validate_file_extension(self, filename: str, allowed_types: list[str] | None = None) -> bool:
        """
        Validates file extension against allowed types.

        Args:
            filename: Filename to validate
            allowed_types: List of allowed file types (image, document, archive)

        Returns:
            True if extension is allowed

        Raises:
            InputSanitizationError: If extension is not allowed
        """
        if not filename:
            raise InputSanitizationError("Filename is empty")

        # Extract extension
        extension = filename.lower()
        extension = "." + extension.split(".")[-1] if "." in extension else ""

        # Determine allowed extensions
        allowed_extensions: set[str] = set()
        if allowed_types is None:
            for ext_set in self.ALLOWED_EXTENSIONS.values():
                allowed_extensions.update(ext_set)
        else:
            for file_type in allowed_types:
                if file_type in self.ALLOWED_EXTENSIONS:
                    allowed_extensions.update(self.ALLOWED_EXTENSIONS[file_type])

        if extension not in allowed_extensions:
            raise InputSanitizationError(
                f"File extension '{extension}' not allowed. Allowed: {list(allowed_extensions)}"
            )

        return True

    def sanitize_url(self, url: str) -> str:
        """
        Sanitizes URL by validating format and removing dangerous protocols.

        Args:
            url: Input URL

        Returns:
            Sanitized URL

        Raises:
            InputSanitizationError: If URL is invalid or dangerous
        """
        if not isinstance(url, str):
            raise InputSanitizationError(f"Expected string, got {type(url).__name__}")

        # Check length
        if len(url) > self.MAX_LENGTHS["url"]:
            raise InputSanitizationError(f"URL too long: {len(url)} > {self.MAX_LENGTHS['url']}")

        # Parse URL
        try:
            parsed = urlparse(url)
        except ValueError as e:
            raise InputSanitizationError(f"Invalid URL format: {e}") from e

        # Check protocol
        dangerous_protocols = {"javascript", "vbscript", "data", "file"}
        if parsed.scheme.lower() in dangerous_protocols:
            raise InputSanitizationError(f"Dangerous protocol: {parsed.scheme}")

        # Only allow HTTP/HTTPS
        if parsed.scheme not in {"http", "https"}:
            raise InputSanitizationError(f"Protocol not allowed: {parsed.scheme}")

        # Validate hostname
        if not parsed.netloc:
            raise InputSanitizationError("Missing hostname")

        hostname = parsed.hostname
        if not hostname:
            # Fallback if parsed.hostname is None but netloc exists
            hostname = parsed.netloc.split(":")[0]
            # Remove brackets if present (for IPv6)
            if hostname.startswith("[") and hostname.endswith("]"):
                hostname = hostname[1:-1]

        # Check for localhost
        if hostname.lower() == "localhost":
            raise InputSanitizationError("Localhost URLs not allowed")

        # DNS resolution to catch SSRF via DNS rebinding / wildcard DNS (e.g. nip.io)
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for _family, _type, _proto, _canonname, sockaddr in addr_info:
                ip = sockaddr[0]
                ip_obj = ipaddress.ip_address(ip)
                if (
                    ip_obj.is_private
                    or ip_obj.is_loopback
                    or ip_obj.is_link_local
                    or ip_obj.is_multicast
                    or ip_obj.is_unspecified
                ):
                    raise InputSanitizationError(f"URL resolves to protected IP: {ip}")
        except socket.gaierror:
            # If we can't resolve it, we shouldn't necessarily block it here,
            # but if it fails to resolve it likely isn't a valid external URL either.
            pass
        except ValueError:
            # Catch IP address parsing errors
            pass

        # Check for IP addresses directly just in case (though getaddrinfo catches them too)
        try:
            # This handles both IPv4 and IPv6
            ipaddress.ip_address(hostname)
            # If we are here, it IS an IP address.
            # Current policy: Block ALL IP addresses.
            raise InputSanitizationError(f"IP addresses not allowed in URLs: {hostname}")
        except ValueError:
            # Not an IP address, continue
            pass

        return url

    def sanitize_query(self, query: str) -> str:
        """
        Sanitizes search query input.

        Args:
            query: Search query to sanitize

        Returns:
            Sanitized query

        Raises:
            InputSanitizationError: If query is invalid
        """
        if not isinstance(query, str):
            raise InputSanitizationError(f"Expected string, got {type(query).__name__}")

        # Check length
        if len(query) > self.MAX_LENGTHS["query"]:
            raise InputSanitizationError(f"Query too long: {len(query)} > {self.MAX_LENGTHS['query']}")

        # Remove dangerous patterns
        sanitized = query
        for pattern in self.compiled_patterns:
            sanitized = pattern.sub("", sanitized)

        # Remove control characters
        sanitized = "".join(char for char in sanitized if ord(char) >= 32)

        # Normalize whitespace
        sanitized = re.sub(r"\s+", " ", sanitized).strip()

        if not sanitized:
            raise InputSanitizationError("Query contains only dangerous content")

        return sanitized

    def validate_telegram_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """
        Validates and sanitizes Telegram message data.

        Args:
            message: Telegram message dictionary

        Returns:
            Sanitized message data

        Raises:
            InputSanitizationError: If message contains invalid data
        """
        if not isinstance(message, dict):
            raise InputSanitizationError(f"Expected dict, got {type(message).__name__}")

        sanitized = {}

        # Validate required fields
        required_fields = ["message_id", "from", "chat", "date"]
        for field in required_fields:
            if field not in message:
                raise InputSanitizationError(f"Missing required field: {field}")

        # Sanitize text content
        if "text" in message:
            sanitized["text"] = self.sanitize_text(message["text"])

        # Sanitize caption
        if "caption" in message:
            sanitized["caption"] = self.sanitize_text(message["caption"])

        # Validate user data
        if "from" in message:
            user = message["from"]
            if not isinstance(user, dict):
                raise InputSanitizationError("Invalid user data")

            sanitized["from"] = {  # type: ignore[assignment]  # mixed value types in dict
                "id": user.get("id"),
                "username": (self.sanitize_text(user.get("username", ""), 32) if user.get("username") else None),
                "first_name": (self.sanitize_text(user.get("first_name", ""), 64) if user.get("first_name") else None),
                "last_name": (self.sanitize_text(user.get("last_name", ""), 64) if user.get("last_name") else None),
            }

        # Validate chat data
        if "chat" in message:
            chat = message["chat"]
            if not isinstance(chat, dict):
                raise InputSanitizationError("Invalid chat data")

            sanitized["chat"] = {  # type: ignore[assignment]  # mixed value types in dict
                "id": chat.get("id"),
                "type": chat.get("type"),
                "title": (self.sanitize_text(chat.get("title", ""), 255) if chat.get("title") else None),
                "username": (self.sanitize_text(chat.get("username", ""), 32) if chat.get("username") else None),
            }

        # Copy other safe fields
        safe_fields = [
            "message_id",
            "date",
            "edit_date",
            "forward_from",
            "reply_to_message",
        ]
        for field in safe_fields:
            if field in message:
                sanitized[field] = message[field]

        return sanitized

    def validate_document_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """
        Validates document metadata.

        Args:
            metadata: Document metadata dictionary

        Returns:
            Validated metadata

        Raises:
            InputSanitizationError: If metadata is invalid
        """
        if not isinstance(metadata, dict):
            raise InputSanitizationError(f"Expected dict, got {type(metadata).__name__}")

        validated = {}

        # Validate filename
        if "filename" in metadata:
            validated["filename"] = self.sanitize_filename(metadata["filename"])

        # Validate file size
        if "file_size" in metadata:
            file_size = metadata["file_size"]
            if not isinstance(file_size, int) or file_size <= 0:
                raise InputSanitizationError("Invalid file size")
            if file_size > 50 * 1024 * 1024:  # 50MB limit
                raise InputSanitizationError("File too large")
            validated["file_size"] = file_size  # type: ignore[assignment]  # dynamic dict value

        # Validate MIME type
        if "mime_type" in metadata:
            mime_type = metadata["mime_type"]
            if not isinstance(mime_type, str):
                raise InputSanitizationError("Invalid MIME type")

            # Check against allowed MIME types
            allowed_mime_types = {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/bmp",
                "image/webp",
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain",
                "application/rtf",
                "application/zip",
                "application/x-rar-compressed",
                "application/x-7z-compressed",
            }

            if mime_type not in allowed_mime_types:
                raise InputSanitizationError(f"MIME type not allowed: {mime_type}")

            validated["mime_type"] = mime_type

        return validated


# Global sanitizer instance
input_sanitizer = InputSanitizer()


def sanitize_input(input_data: Any, input_type: str = "text", **kwargs) -> Any:
    """
    Convenience function for sanitizing different types of input.

    Args:
        input_data: Input data to sanitize
        input_type: Type of input ('text', 'filename', 'url', 'query', 'telegram_message', 'document_metadata')
        **kwargs: Additional arguments for specific sanitizers

    Returns:
        Sanitized input data

    Raises:
        InputSanitizationError: If input cannot be sanitized
    """
    try:
        if input_type == "text":
            return input_sanitizer.sanitize_text(input_data, **kwargs)
        elif input_type == "filename":
            return input_sanitizer.sanitize_filename(input_data)
        elif input_type == "url":
            return input_sanitizer.sanitize_url(input_data)
        elif input_type == "query":
            return input_sanitizer.sanitize_query(input_data)
        elif input_type == "telegram_message":
            return input_sanitizer.validate_telegram_message(input_data)
        elif input_type == "document_metadata":
            return input_sanitizer.validate_document_metadata(input_data)
        else:
            raise InputSanitizationError(f"Unknown input type: {input_type}")
    except Exception as e:
        if isinstance(e, InputSanitizationError):
            raise
        raise InputSanitizationError(f"Sanitization failed: {e}") from e


# ============================================================================
# RATE LIMITING
# ============================================================================


class RateLimiter:
    """Sliding-window per-user rate limiter with periodic cleanup."""

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        """
        Args:
            max_requests: Maximum number of requests per window
            window_seconds: Window size in seconds
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._user_requests: dict[int, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._cleanup_interval = 300  # Cleanup every 5 minutes
        self._last_cleanup = time.time()

    async def check_rate_limit(self, user_id: int) -> bool:
        """
        Check if a user has exceeded their request rate limit.

        Args:
            user_id: User ID

        Returns:
            True if request is allowed, False if rate limited
        """
        current_time = time.time()

        async with self._lock:
            # Periodically clean up old entries
            if current_time - self._last_cleanup > self._cleanup_interval:
                await self._cleanup_old_entries(current_time)
                self._last_cleanup = current_time

            # Get user's request list
            user_requests = self._user_requests[user_id]

            # Remove requests older than the window
            cutoff_time = current_time - self.window_seconds
            user_requests[:] = [req_time for req_time in user_requests if req_time > cutoff_time]

            # Check limit
            if len(user_requests) >= self.max_requests:
                logging.warning(
                    "Rate limit exceeded for user %s: %s/%s requests",
                    user_id,
                    len(user_requests),
                    self.max_requests,
                )
                return False

            # Record current request
            user_requests.append(current_time)
            return True

    async def _cleanup_old_entries(self, current_time: float):
        """Remove stale entries to conserve memory."""
        cutoff_time = current_time - self.window_seconds
        users_to_remove = []

        for user_id, requests in self._user_requests.items():
            # Remove stale requests
            self._user_requests[user_id] = [req_time for req_time in requests if req_time > cutoff_time]

            # Mark users with no active requests for removal
            if not self._user_requests[user_id]:
                users_to_remove.append(user_id)

        for user_id in users_to_remove:
            del self._user_requests[user_id]

        if users_to_remove:
            logging.debug("Cleaned up %d inactive users from rate limiter", len(users_to_remove))

    async def get_user_stats(self, user_id: int) -> dict[str, Any]:
        """Return request statistics for a user."""
        current_time = time.time()
        cutoff_time = current_time - self.window_seconds

        async with self._lock:
            user_requests = self._user_requests.get(user_id, [])
            recent_requests = [req_time for req_time in user_requests if req_time > cutoff_time]

            return {
                "user_id": user_id,
                "requests_count": len(recent_requests),
                "max_requests": self.max_requests,
                "window_seconds": self.window_seconds,
                "remaining": max(0, self.max_requests - len(recent_requests)),
                "reset_at": (min(recent_requests) + self.window_seconds if recent_requests else current_time),
            }

    async def reset_user_limit(self, user_id: int):
        """Reset rate limit for a user (admin action)."""
        async with self._lock:
            if user_id in self._user_requests:
                del self._user_requests[user_id]
                logging.info("Rate limit reset for user %s", user_id)


class SyncRateLimiter:
    """Synchronous sliding-window rate limiter keyed by arbitrary string (IP, token, etc.).

    Mirrors the algorithm of the async ``RateLimiter`` above but uses a
    ``threading.Lock`` so it can be called from non-async Quart helpers.
    """

    def __init__(self, max_requests: int = 5, window_seconds: int = 300, cleanup_every: int = 50):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()
        self._cleanup_every = cleanup_every
        self._call_count = 0

    # ------------------------------------------------------------------

    def check(self, key: str) -> bool:
        """Return *True* if the request is allowed, *False* if rate-limited.

        Does **not** record the attempt — call :meth:`record` separately so
        that successful vs. failed events can be tracked independently.
        """
        with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            self._requests[key] = [t for t in self._requests[key] if t > cutoff]
            self._maybe_cleanup(cutoff)
            return len(self._requests[key]) < self.max_requests

    def record(self, key: str) -> None:
        """Record one event against *key*."""
        with self._lock:
            self._requests[key].append(time.time())

    # ------------------------------------------------------------------

    def _maybe_cleanup(self, cutoff: float) -> None:
        self._call_count += 1
        if self._call_count < self._cleanup_every:
            return
        self._call_count = 0
        stale = [k for k, v in self._requests.items() if not v or v[-1] <= cutoff]
        for k in stale:
            del self._requests[k]


# Global rate limiter instance
# Settings: 30 requests per minute by default
rate_limiter = RateLimiter(max_requests=30, window_seconds=60)


async def check_user_rate_limit(user_id: int) -> bool:
    """
    Check rate limit for a user.

    Args:
        user_id: User ID

    Returns:
        True if request is allowed, False if rate limited
    """
    return await rate_limiter.check_rate_limit(user_id)
