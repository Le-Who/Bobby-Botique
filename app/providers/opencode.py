"""Opencode Go AI provider — OpenAI-compatible endpoint at opencode.ai/zen/go/v1.

Subclasses OpenRouterProvider, inheriting:
- Gemini → OpenAI message format conversion (_build_messages)
- Multimodal image handling (_build_image_part)
- SSE streaming loop (stream_response)
- Error tag handling

Only overrides: base URL, request headers, and model-name prefix stripping.
"""

from typing import Any

from app.providers.base import AIResponse
from app.providers.openrouter import OpenRouterProvider
from app.request_context import get_request_id


class OpencodeGoProvider(OpenRouterProvider):
    """Opencode Go AI provider — OpenAI-compatible endpoint.

    Uses standard Bearer authentication, no OpenRouter-specific headers.
    Model slugs are sent without the ``opencode-go/`` prefix used internally
    for routing disambiguation.
    """

    provider_name = "opencode"

    # Endpoint — Opencode Go OpenAI-compatible chat completions path
    _BASE_URL = "https://opencode.ai/zen/go/v1/chat/completions"

    def _get_url(self) -> str:
        return self._BASE_URL

    def _get_headers(self) -> dict[str, str]:
        """Standard Bearer auth — no OpenRouter-specific headers."""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _strip_model_prefix(self, model_name: str) -> str:
        """Strip internal ``opencode-go/`` routing prefix before sending to API."""
        return model_name.removeprefix("opencode-go/")

    # _execute_request and stream_response are inherited from OpenRouterProvider
    # Both call self._get_url(), self._get_headers(), self._strip_model_prefix()
    # so all request differences are captured above.

    def _log_failure(
        self,
        start_time: Any,
        model: str,
        msg: str,
        user_id: Any,
        chat_id: Any,
    ) -> None:
        """Log provider-specific failure for Opencode Go requests."""
        import logging

        logging.error(
            "Opencode Go request failed: model=%s user=%s chat=%s error=%s",
            model,
            user_id,
            chat_id,
            msg,
        )
