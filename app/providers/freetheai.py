"""FreeTheAI chat provider — OpenAI-compatible router.

Uses ``https://api.freetheai.xyz/v1/chat/completions`` with Bearer auth.
Model slugs (e.g. ``cat/claude-4-6-sonnet``, ``yng/gemini-3-1-pro``) are
sent as-is — no prefix stripping required.

Inherits streaming, error handling, and message formatting from
:class:`OpenRouterProvider`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.errors import ErrorCode, tag_error
from app.providers.openrouter import OpenRouterProvider
from app.request_context import get_request_id


class FreeTheAIProvider(OpenRouterProvider):
    """FreeTheAI chat provider — subclass of OpenRouterProvider."""

    provider_name = "freetheai"

    _CHAT_URL = "https://api.freetheai.xyz/v1/chat/completions"

    def _get_url(self) -> str:
        return self._CHAT_URL

    def _get_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id
        return headers

    def _strip_model_prefix(self, model_name: str) -> str:
        """FreeTheAI uses full slugs (e.g. cat/claude-4-6-sonnet) — no stripping."""
        return model_name

    def _is_model_access_denied(self, status: int, response_text: str) -> bool:
        if status != 403:
            return False
        body = re.sub(r"[\W_]+", " ", response_text.lower())
        credential = r"(?:api key|access token|token|credentials?)"
        rejected = r"(?:invalid|expired|revoked|missing|incorrect)"
        if re.search(rf"\b{rejected} {credential}\b|\b{credential} (?:is )?{rejected}\b", body):
            return False
        return bool(re.search(r"\b(?:model|tier)\b", body))

    def _build_http_error_tag(
        self,
        status: int,
        response_text: str,
        model_name: str,
    ) -> str:
        """Map FTA HTTP errors to user-friendly messages."""
        if status == 401:
            return tag_error(ErrorCode.INVALID_KEY, "🔑 Неверный FreeTheAI ключ. Проверьте FREETHEAI_API_KEYS.")
        if status == 403:
            if self._is_model_access_denied(status, response_text or ""):
                logging.warning(
                    "FreeTheAI rejected model access: model=%s status=%s response_chars=%s",
                    model_name,
                    status,
                    len(response_text or ""),
                )
                return tag_error(
                    ErrorCode.INVALID_REQUEST,
                    "❌ FreeTheAI отклонил доступ к этой модели. Возможно, требуется более высокий тир.",
                )
            return tag_error(ErrorCode.INVALID_KEY, "🔑 Доступ к FreeTheAI запрещён.")
        if status == 429:
            return tag_error(ErrorCode.RATE_LIMIT, "⏱️ Лимит FreeTheAI исчерпан. Подождите немного.")

        return super()._build_http_error_tag(status, response_text, model_name)

    def _extra_payload_params(self, model_name: str, thinking_level: str | None) -> dict[str, Any]:
        """FreeTheAI does not support reasoning_effort or tools injection."""
        return {}
