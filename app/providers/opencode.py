"""Opencode Go AI provider — OpenAI-compatible endpoint at opencode.ai/zen/go/v1.

Subclasses OpenRouterProvider, inheriting:
- Gemini → OpenAI message format conversion (_build_messages)
- Multimodal image handling (_build_image_part)
- SSE streaming loop (stream_response)
- Error tag handling

Only overrides: base URL, request headers, model-name prefix stripping,
and HTTP error mapping where Opencode semantics differ from OpenRouter.
"""

import logging
from typing import Any

from app.errors import ErrorCode, tag_error
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

    def _build_http_error_tag(
        self,
        status: int,
        response_text: str,
        model_name: str,
    ) -> str:
        """Avoid treating model-specific 401/403 responses as broken credentials.

        Opencode can reject a specific model with 401/403 while the same key still
        succeeds on other models. Only explicit invalid-key wording should map
        to INVALID_KEY; model-access failures should stay non-key-related so the
        router can cascade without labeling the key as permanently broken.
        """
        if status in {401, 403}:
            body = (response_text or "").lower()
            invalid_key_markers = (
                "invalid api key",
                "invalid key",
                "api key is invalid",
                "bad api key",
                "bad key",
                "invalid token",
                "token is invalid",
                "authentication failed",
                "auth failed",
            )
            model_access_markers = (
                "model",
                "access",
                "permission",
                "not allowed",
                "not available",
                "unsupported",
                "does not exist",
                "not found",
                "forbidden",
            )
            if any(marker in body for marker in invalid_key_markers):
                return tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный API ключ. Проверьте настройки.")
            if any(marker in body for marker in model_access_markers):
                logging.warning(
                    "Opencode rejected model access without invalidating key: model=%s status=%s body=%s",
                    model_name,
                    status,
                    (response_text or "")[:200],
                )
                return tag_error(
                    ErrorCode.INVALID_REQUEST,
                    "❌ Opencode отклонил доступ к этой модели для текущего ключа.",
                )
            logging.warning(
                "Opencode returned ambiguous auth error; treating as model/request access: model=%s status=%s body=%s",
                model_name,
                status,
                (response_text or "")[:200],
            )
            return tag_error(
                ErrorCode.INVALID_REQUEST,
                "❌ Opencode отклонил этот запрос для текущего ключа или модели.",
            )
        return super()._build_http_error_tag(status, response_text, model_name)

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
        logging.error(
            "Opencode Go request failed: model=%s user=%s chat=%s error=%s",
            model,
            user_id,
            chat_id,
            msg,
        )
