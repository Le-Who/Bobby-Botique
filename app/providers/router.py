"""
ProviderRouter — routes AI requests with key rotation and health scoring.

Also provides the module-level convenience functions:
- get_provider_router() → singleton
- get_ai_response()     → backward-compat wrapper
"""

import logging
from typing import Any

from app.config import settings
from app.errors import (
    ErrorCode,
    classify_key_error,
    is_error_message,
    is_key_related_error,
    tag_error,
)
from app.providers.openrouter import _has_multimodal_content


class ProviderRouter:
    """
    Routes AI requests to the right provider with key rotation and health scoring.

    Uses DB-backed KeyStatusManager for persistent per-model key health tracking.
    Keys are suspended with error-category-aware cooldowns and automatically
    recover after their cooldown expires (two-tier selection in SQL).
    """

    def __init__(self, rate_limit_per_minute: int = 20) -> None:
        # Use the consolidated RateLimiter from security.py (includes periodic cleanup)
        from app.security import RateLimiter

        self._rate_limiter = RateLimiter(max_requests=rate_limit_per_minute, window_seconds=60)

    async def get_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
    ) -> tuple[str, int | None]:
        """
        Get AI response with automatic key rotation and health-aware selection.

        Delegates to AgentRequestUseCase for key resolution, which uses
        two-tier SQL (active first, then cooldown-expired) to pick keys.
        On failure, classifies the error and suspends the key with appropriate
        cooldown. On success, promotes the key back to active.
        """
        from app.agent_use_cases import AgentRequestUseCase
        from app.repos.keys import get_key_status_manager

        # Per-user rate limiting (async — RateLimiter from security.py)
        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            return (
                tag_error(
                    ErrorCode.RATE_LIMIT,
                    "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
                ),
                None,
            )

        # Auto-detect multimodal content → force Gemini
        if use_openrouter is None and _has_multimodal_content(history):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True  # Track if ALL failures are permanent (model-level)

        for attempt in range(max_key_retries):
            key_data, model_used, resolution = await use_case.resolve_ai_request(
                preferred_model,
                use_openrouter=use_openrouter,
                excluded_key_hashes=failed_keys,
            )

            if not key_data:
                if resolution == "all_exhausted":
                    is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model)
                    provider_name = "OpenRouter" if is_or else "Gemini"
                    return (
                        tag_error(
                            ErrorCode.KEYS_EXHAUSTED,
                            f"🚫 Все ключи {provider_name} недоступны или исчерпаны. Попробуйте позже.",
                        ),
                        None,
                    )
                if resolution == "no_keys":
                    return (
                        tag_error(
                            ErrorCode.NO_KEYS,
                            "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                        ),
                        None,
                    )
                if resolution == "decryption_failed":
                    return (
                        tag_error(
                            ErrorCode.DECRYPTION_FAILED,
                            "🔐 Ошибка расшифровки API-ключей. Обратитесь к администратору (возможно, изменился ADMIN_SECRET).",
                        ),
                        None,
                    )
                return (
                    tag_error(
                        ErrorCode.KEYS_EXHAUSTED,
                        "🚫 Не удалось получить доступный ключ API. Попробуйте позже.",
                    ),
                    None,
                )

            # Execute the request
            assert model_used is not None  # guaranteed by _resolve_ai_request
            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                thinking_level=thinking_level,
            )

            # Track health based on response
            if response_text and is_error_message(response_text) and is_key_related_error(response_text):
                failed_keys.add(key_data["key_hash"])
                error_category = classify_key_error(response_text)

                if error_category != "permanent":
                    all_permanent = False

                if error_category != "transient":
                    try:
                        await status_mgr.suspend_key(
                            key_data["key_hash"],
                            model_used,  # type: ignore[arg-type]  # asserted above
                            error_category,
                            response_text[:200],
                        )
                    except Exception as e:
                        logging.warning(
                            "Non-critical: failed to suspend key: %s",
                            e,
                        )

                logging.warning(
                    "Key %s… failed (category=%s, attempt %d/%d). Error: %s",
                    key_data["key_hash"][:8],
                    error_category,
                    attempt + 1,
                    max_key_retries,
                    response_text[:100],
                )
                continue

            # Success — update health and increment usage
            if response_text and not is_error_message(response_text):
                try:
                    await status_mgr.record_success(
                        key_data["key_hash"],
                        model_used,  # type: ignore[arg-type]  # asserted above
                    )
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)

                try:
                    await use_case.increment_key_usage(key_data["key_hash"], model_used, use_openrouter)  # type: ignore[arg-type]  # asserted above
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)

            return response_text, token_count

        # ── Model-level fallback ─────────────────────────────────────────
        # All keys failed for the preferred model. If every failure was
        # "permanent" (API_KEY_INVALID — Google rejects the key for this
        # specific model), try alternative models before giving up.
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model,
                history,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                use_case,
                status_mgr,
            )
            if fallback_result is not None:
                return fallback_result

        is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        provider_name = "OpenRouter" if is_or else "Gemini"
        return (
            tag_error(
                ErrorCode.KEYS_EXHAUSTED,
                f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
            ),
            None,
        )

    async def stream_response(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
        thinking_level: str | None = None,
    ):
        """
        Stream AI response with automatic key rotation.
        Yields chunks of text.
        """
        from app.agent_use_cases import AgentRequestUseCase
        from app.providers.base import get_provider_for_model
        from app.repos.keys import get_key_status_manager

        if user_id and not await self._rate_limiter.check_rate_limit(user_id):
            yield tag_error(
                ErrorCode.RATE_LIMIT,
                "⏳ Слишком много запросов. Пожалуйста, подождите минуту.",
            )
            return

        if use_openrouter is None and _has_multimodal_content(history):
            use_openrouter = False

        use_case = AgentRequestUseCase()
        status_mgr = get_key_status_manager()
        failed_keys: set[str] = set()
        all_permanent: bool = True

        for _attempt in range(max_key_retries):
            key_data, model_used, _resolution = await use_case.resolve_ai_request(
                preferred_model,
                use_openrouter=use_openrouter,
                excluded_key_hashes=failed_keys,
            )

            if not key_data:
                is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model)
                provider_name = "OpenRouter" if is_or else "Gemini"
                yield tag_error(
                    ErrorCode.KEYS_EXHAUSTED,
                    f"🚫 Все ключи {provider_name} недоступны.",
                )
                return

            assert model_used is not None
            provider = get_provider_for_model(model_used, key_data["api_key"])

            stream_started = False
            try:
                # We yield from the provider's stream
                async for chunk in provider.stream_response(  # type: ignore[attr-defined]
                    history=history,
                    model_name=model_used,
                    system_instruction=system_instruction,
                    thinking_level=thinking_level,
                ):
                    if not stream_started:
                        stream_started = True
                        # Once we start receiving chunks, we consider the key successful
                        try:
                            await status_mgr.record_success(key_data["key_hash"], model_used)
                            await use_case.increment_key_usage(key_data["key_hash"], model_used, use_openrouter)
                        except Exception as e:
                            logging.debug("Non-critical stats update failed: %s", e)

                    yield chunk

                # If we successfully completed the stream, exit the retry loop
                if stream_started:
                    return

            except Exception as e:
                # If the stream failed BEFORE yielding anything, we can retry with another key.
                # If it failed mid-stream, we must abort because the user already saw partial text.
                if stream_started:
                    logging.error("Stream failed mid-flight: %s", e)
                    yield f"\n\n[Ошибка трансляции: {str(e)}]"
                    return

                # Stream didn't start, so this key is bad. Suspend and loop.
                error_msg = str(e)
                failed_keys.add(key_data["key_hash"])
                all_permanent = False
                try:
                    await status_mgr.suspend_key(
                        key_data["key_hash"],
                        model_used,
                        "transient",  # assume stream setup failures are transient
                        error_msg[:200],
                    )
                except Exception as db_e:
                    logging.warning("Failed to suspend key: %s", db_e)

                continue

        # Exhausted retries
        is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        provider_name = "OpenRouter" if is_or else "Gemini"
        yield tag_error(
            ErrorCode.KEYS_EXHAUSTED,
            f"🚫 Все доступные ключи {provider_name} не сработали.",
        )

        # ── Model-level fallback ─────────────────────────────────────────
        # All keys failed for the preferred model. If every failure was
        # "permanent" (API_KEY_INVALID — Google rejects the key for this
        # specific model), try alternative models before giving up.
        if all_permanent and failed_keys:
            fallback_result = await self._try_model_fallback(
                preferred_model,
                history,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
                use_case,
                status_mgr,
            )
            if fallback_result is not None:
                yield fallback_result[0]
                return

        is_or = use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        provider_name = "OpenRouter" if is_or else "Gemini"
        yield tag_error(
            ErrorCode.KEYS_EXHAUSTED,
            f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
        )
        return

    async def _try_model_fallback(
        self,
        failed_model: str,
        history: list,
        system_instruction: str | None,
        user_id: int | None,
        chat_id: int | None,
        use_openrouter: bool | None,
        use_case,
        status_mgr,
    ) -> tuple[str, int | None] | None:
        """Try fallback models when all keys fail with permanent errors for one model.

        Returns (response_text, token_count) on success, or None if no fallback works.
        """
        from app.errors import is_error_message

        is_or = use_openrouter if use_openrouter is not None else ("/" in failed_model)
        fallback_models = settings.OPENROUTER_AVAILABLE_MODELS if is_or else settings.AVAILABLE_MODELS

        for fallback_model in fallback_models:
            if fallback_model == failed_model:
                continue

            key_data, model_used, _ = await use_case.resolve_ai_request(
                fallback_model,
                use_openrouter=use_openrouter,
            )
            if not key_data:
                continue

            logging.info(
                "Model fallback: trying %s instead of %s (all keys rejected by API for original model)",
                fallback_model,
                failed_model,
            )

            response_text, token_count = await use_case.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
            )

            if response_text and not is_error_message(response_text):
                logging.info(
                    "Model fallback succeeded: %s → %s",
                    failed_model,
                    model_used,
                )
                try:
                    await status_mgr.record_success(key_data["key_hash"], model_used)
                except Exception as e:
                    logging.debug("Non-critical: record_success failed: %s", e)
                try:
                    await use_case.increment_key_usage(
                        key_data["key_hash"],
                        model_used,
                        use_openrouter,
                    )
                except Exception as e:
                    logging.warning("Non-critical: failed to increment key usage: %s", e)
                return response_text, token_count

            logging.warning(
                "Model fallback %s also failed: %s",
                fallback_model,
                (response_text or "")[:100],
            )

        return None

    async def get_key_stats(self) -> list[dict[str, Any]]:
        """Return health stats for all tracked keys (for diagnostics)."""
        from app.repos.keys import get_key_status_manager

        return await get_key_status_manager().get_all_statuses()


# Module-level singleton
_provider_router: ProviderRouter | None = None


def get_provider_router() -> ProviderRouter:
    """Get the singleton ProviderRouter instance."""
    global _provider_router
    if _provider_router is None:
        _provider_router = ProviderRouter()
    return _provider_router


async def get_ai_response(
    api_key: str,
    history: list[dict[str, Any]],
    model_name: str,
    system_instruction: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    max_retries: int = 3,
) -> tuple[str, int | None]:
    """
    Unified entry point for AI responses.

    Automatically selects the appropriate provider based on model name.
    Returns tuple (response_text, token_count) for backwards compatibility.

    Args:
        api_key: API key for the provider
        history: Message history
        model_name: Model identifier (Gemini or OpenRouter format)
        system_instruction: Optional system prompt
        user_id: User ID for logging
        chat_id: Chat ID for logging
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (response_text, token_count)
    """
    from app.providers.base import get_provider_for_model

    provider = get_provider_for_model(model_name, api_key)

    response = await provider.get_response(
        history=history,
        model_name=model_name,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
        max_retries=max_retries,
    )

    return response.text, response.token_count if response.success else None
