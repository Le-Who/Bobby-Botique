import logging
import os
import time
import traceback
from datetime import datetime
from typing import Any

from app.request_context import get_chat_id, get_request_id, get_user_id
from app.utils.json_compat import json

# Emoji lookup for log prefixes
_EMOJI_REQUEST = {
    "gemini": "🤖",
    "openrouter": "🔀",
    "tavily": "🔍",
    "telegram": "📱",
}
_EMOJI_OK = "✅"
_EMOJI_FAIL = "❌"
_EMOJI_ERROR = "💥"


class APILogger:
    """Unified API request/response logger.

    All API interactions (Gemini, OpenRouter, Tavily, Telegram) are logged
    through two generic methods: ``log_request`` and ``log_response``.
    Context fields (request_id, user_id, chat_id) are pulled automatically
    from ``contextvars`` — callers never need to pass them.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("api_logger")
        self.logger.setLevel(logging.INFO)
        self._pretty = os.environ.get("LOG_PRETTY", "").lower() in (
            "1",
            "true",
            "yes",
        )

    # ── core helpers ─────────────────────────────────────────────────────

    def _format_log(self, data: dict[str, Any]) -> str:
        """Format log payload — pretty-printed in dev, compact in prod."""
        if self._pretty:
            return json.dumps(data, ensure_ascii=False, indent=2, default=str)
        return json.dumps(data, ensure_ascii=False, default=str)

    @staticmethod
    def _context_fields() -> dict[str, Any]:
        """Return context fields sourced from contextvars."""
        return {
            "request_id": get_request_id(),
            "user_id": get_user_id(),
            "chat_id": get_chat_id(),
        }

    # ── public API ───────────────────────────────────────────────────────

    def log_request(self, api: str, **fields: Any) -> float:
        """Log the start of an API request. Returns ``time.time()`` for duration tracking.

        Usage::

            start = api_logger.log_request("gemini", model="gemini-2.5-flash", prompt_length=1200)
        """
        data: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "api": api,
            **self._context_fields(),
            "status": "STARTED",
            **fields,
        }
        emoji = _EMOJI_REQUEST.get(api, "📡")
        self.logger.info("%s %s REQUEST STARTED: %s", emoji, api.upper(), self._format_log(data))
        return time.time()

    def log_response(
        self,
        api: str,
        start_time: float,
        *,
        success: bool = True,
        error_message: str | None = None,
        **fields: Any,
    ) -> float:
        """Log the completion of an API request. Returns elapsed seconds.

        Usage::

            api_logger.log_response("gemini", start_time, model="gemini-2.5-flash",
                                     response_length=4500, token_count=1200)
        """
        if not isinstance(start_time, (int, float)) or start_time <= 0:
            logging.warning("Invalid start_time in log_response(%s): %s", api, start_time)
            start_time = time.time()

        duration = time.time() - start_time
        data: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "api": api,
            "duration_ms": round(duration * 1000, 2),
            **self._context_fields(),
            "success": success,
            "error_message": error_message,
            "status": "COMPLETED",
            **fields,
        }

        if success:
            self.logger.info(
                "%s %s RESPONSE COMPLETED: %s",
                _EMOJI_OK,
                api.upper(),
                self._format_log(data),
            )
        else:
            self.logger.error(
                "%s %s RESPONSE FAILED: %s",
                _EMOJI_FAIL,
                api.upper(),
                self._format_log(data),
            )

        return duration

    def log_error(
        self,
        api: str,
        error: Exception,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Log an API error with full traceback.

        Usage::

            api_logger.log_error("gemini", exc, context={"model": "gemini-2.5-flash"})
        """
        error_data: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "api": api,
            **self._context_fields(),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc(),
            "context": context,
            "status": "ERROR",
        }
        self.logger.error("%s API ERROR: %s", _EMOJI_ERROR, self._format_log(error_data))


# Global singleton
api_logger = APILogger()
