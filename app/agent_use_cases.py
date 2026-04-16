import hashlib
import logging
from typing import Any

from app.config import get_opencode_keys, get_openrouter_keys, get_use_openrouter, settings
from app.providers.base import is_opencode_model
from app.repos.keys import (
    get_available_gemini_key,
    get_available_openrouter_key,
    increment_gemini_key_usage,
    increment_openrouter_key_usage,
    invalidate_key_cache,
)


class AgentRequestUseCase:
    async def resolve_ai_request(
        self,
        preferred_model: str,
        use_openrouter: bool | None = None,
        excluded_key_hashes: set[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        excluded = excluded_key_hashes or set()

        # Opencode Go models take priority (they also contain '/')
        if is_opencode_model(preferred_model):
            return await self._resolve_opencode_request(preferred_model, excluded)

        if use_openrouter is None:
            use_openrouter = "/" in preferred_model or get_use_openrouter()

        if use_openrouter and not get_openrouter_keys():
            logging.warning("OpenRouter model %s selected but no keys available", preferred_model)
            return None, None, "no_keys"

        if use_openrouter or "/" in preferred_model:
            return await self._resolve_openrouter_request(preferred_model, excluded)

        return await self._resolve_gemini_request(preferred_model, excluded)

    async def _resolve_key_generic(
        self,
        preferred_model: str,
        get_key_func: Any,
        fallback_priority: list[str],
        excluded_key_hashes: set[str] | None = None,
        invalidate_cache_func: Any = None,
        provider_name: str = "Unknown",
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        from app.errors import DecryptionError

        excluded = excluded_key_hashes or set()

        # Invalidate cache when exclusions exist (caller retrying after failure)
        if excluded and invalidate_cache_func:
            await invalidate_cache_func(preferred_model)

        # Try preferred model — DB query already filters excluded + suspended
        try:
            key = await get_key_func(preferred_model, excluded_hashes=excluded)
        except DecryptionError as e:
            logging.error(
                "Cannot decrypt %s API key: %s — check ADMIN_SECRET",
                provider_name,
                e,
            )
            return None, None, "decryption_failed"

        if key:
            return key, preferred_model, None

        logging.warning(
            f"All keys for preferred model {preferred_model} are exhausted or excluded. Attempting fallback."
        )

        # Try fallback models
        for fallback_model in fallback_priority:
            if fallback_model == preferred_model:
                continue
            if excluded and invalidate_cache_func:
                await invalidate_cache_func(fallback_model)

            try:
                key = await get_key_func(fallback_model, excluded_hashes=excluded)
            except DecryptionError as e:
                logging.error(
                    "Cannot decrypt %s API key: %s — check ADMIN_SECRET",
                    provider_name,
                    e,
                )
                return None, None, "decryption_failed"

            if key:
                logging.info("Found available fallback key for model %s.", fallback_model)
                return key, fallback_model, "confirm_fallback"

        logging.error("All %s API keys for all models are exhausted or excluded.", provider_name)
        return None, None, "all_exhausted"

    async def _resolve_gemini_request(
        self, preferred_model: str, excluded_key_hashes: set[str] | None = None
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        fallback_priority = [
            settings.RESEARCH_MODEL,
            settings.DEFAULT_MODEL,
            settings.QNA_MODEL,
        ]
        return await self._resolve_key_generic(
            preferred_model,
            get_available_gemini_key,
            fallback_priority,
            excluded_key_hashes,
            invalidate_key_cache,
            provider_name="Gemini",
        )

    async def _resolve_openrouter_request(
        self, preferred_model: str, excluded_key_hashes: set[str] | None = None
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        model_mapping = {
            settings.DEFAULT_MODEL: settings.OPENROUTER_DEFAULT_MODEL,
            settings.QNA_MODEL: settings.OPENROUTER_QNA_MODEL,
            settings.RESEARCH_MODEL: settings.OPENROUTER_RESEARCH_MODEL,
            settings.URL_SELECTION_MODEL: settings.OPENROUTER_URL_SELECTION_MODEL,
        }

        openrouter_model = (
            preferred_model
            if "/" in preferred_model
            else model_mapping.get(preferred_model, settings.OPENROUTER_DEFAULT_MODEL)
        )
        fallback_priority = [
            settings.OPENROUTER_RESEARCH_MODEL,
            settings.OPENROUTER_DEFAULT_MODEL,
            settings.OPENROUTER_QNA_MODEL,
        ]

        return await self._resolve_key_generic(
            openrouter_model,
            get_available_openrouter_key,
            fallback_priority,
            excluded_key_hashes,
            invalidate_cache_func=None,
            provider_name="OpenRouter",
        )

    async def _resolve_opencode_request(
        self, preferred_model: str, excluded_key_hashes: set[str] | None = None
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """Resolve an Opencode Go API key from the in-memory key pool.

        Keys are stored in ``settings.OPENCODE_API_KEYS`` (comma-separated).
        Exclusion is tracked using the first 16 chars of SHA256(key) to match
        the pattern used by Gemini and OpenRouter key hashes.
        """
        excluded = excluded_key_hashes or set()
        keys = get_opencode_keys()
        if not keys:
            logging.warning("Opencode Go selected but OPENCODE_API_KEYS is empty")
            return None, None, "no_keys"

        for key in keys:
            key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
            if key_hash in excluded:
                continue
            return {"api_key": key, "key_hash": key_hash}, preferred_model, None

        logging.error("All Opencode Go API keys are excluded (exhausted for this request).")
        return None, None, "all_exhausted"

    async def get_ai_response(
        self,
        api_key: str,
        history: list[dict[str, Any]],
        model_name: str,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        thinking_level: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, int | None]:
        if use_openrouter is None:
            use_openrouter = "/" in model_name or get_use_openrouter()

        if use_openrouter and not get_openrouter_keys():
            return (
                "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                None,
            )

        # Unified path: delegate to Provider classes (same path as ProviderRouter)
        from app.providers import get_provider_for_model

        provider = get_provider_for_model(model_name, api_key)
        # Build kwargs, only pass timeout if caller specified one
        kwargs: dict[str, Any] = {
            "history": history,
            "model_name": model_name,
            "system_instruction": system_instruction,
            "user_id": user_id,
            "chat_id": chat_id,
            "thinking_level": thinking_level,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
            kwargs["max_retries"] = 1  # tight budget → no retries
        response = await provider.get_response(**kwargs)

        token_count = response.token_count if response.success else None
        return response.text, token_count

    async def increment_key_usage(self, key_hash: str, model_name: str, use_openrouter: bool | None = None) -> None:
        # Opencode Go: daily usage tracked via DAILY_LIMITS in the DB key system.
        # We skip the openrouter/gemini DB increment for opencode models.
        if is_opencode_model(model_name):
            return
        if use_openrouter is None:
            use_openrouter = "/" in model_name or get_use_openrouter()
        if use_openrouter:
            await increment_openrouter_key_usage(key_hash, model_name)
        else:
            await increment_gemini_key_usage(key_hash, model_name)

    async def get_ai_response_with_key_rotation(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
        use_openrouter: bool | None = None,
        max_key_retries: int = 3,
    ) -> tuple[str, int | None]:
        """Delegate to ProviderRouter for health-aware key rotation.

        This method exists for backward compatibility. All new code should
        use ProviderRouter.get_response() directly via _get_ai_response_with_routing().
        """
        from app.providers import get_provider_router

        router = get_provider_router()
        return await router.get_response(
            preferred_model,
            history,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
            use_openrouter=use_openrouter,
            max_key_retries=max_key_retries,
        )
