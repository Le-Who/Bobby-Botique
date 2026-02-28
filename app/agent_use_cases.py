import logging

from app.config import get_openrouter_keys, get_use_openrouter, settings
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
    ):
        excluded = excluded_key_hashes or set()

        if use_openrouter is None:
            use_openrouter = "/" in preferred_model or get_use_openrouter()

        if use_openrouter and not get_openrouter_keys():
            logging.warning(
                f"OpenRouter model {preferred_model} selected but no keys available"
            )
            return None, None, "no_keys"

        if use_openrouter or "/" in preferred_model:
            return await self._resolve_openrouter_request(preferred_model, excluded)

        return await self._resolve_gemini_request(preferred_model, excluded)

    async def _resolve_key_generic(
        self,
        preferred_model: str,
        get_key_func,
        fallback_priority: list[str],
        excluded_key_hashes: set[str] | None = None,
        invalidate_cache_func=None,
        provider_name: str = "Unknown",
    ):
        from app.errors import DecryptionError

        excluded = excluded_key_hashes or set()
        if excluded and invalidate_cache_func:
            await invalidate_cache_func(preferred_model)

        max_attempts = 5
        for _ in range(max_attempts):
            try:
                key = await get_key_func(preferred_model)
            except DecryptionError as e:
                logging.error(
                    "Cannot decrypt %s API key: %s — check ADMIN_SECRET",
                    provider_name, e,
                )
                return None, None, "decryption_failed"

            if key and key["key_hash"] not in excluded:
                return key, preferred_model, None
            if key and key["key_hash"] in excluded and invalidate_cache_func:
                await invalidate_cache_func(preferred_model)
                continue
            break

        logging.warning(
            f"All keys for preferred model {preferred_model} are exhausted or excluded. Attempting fallback."
        )

        for fallback_model in fallback_priority:
            if fallback_model == preferred_model:
                continue
            if excluded and invalidate_cache_func:
                await invalidate_cache_func(fallback_model)

            for _ in range(max_attempts):
                try:
                    key = await get_key_func(fallback_model)
                except DecryptionError as e:
                    logging.error(
                        "Cannot decrypt %s API key: %s — check ADMIN_SECRET",
                        provider_name, e,
                    )
                    return None, None, "decryption_failed"

                if key and key["key_hash"] not in excluded:
                    logging.info(
                        f"Found available fallback key for model {fallback_model}."
                    )
                    return key, fallback_model, "confirm_fallback"
                if key and key["key_hash"] in excluded and invalidate_cache_func:
                    await invalidate_cache_func(fallback_model)
                    continue
                break

        logging.error(
            f"All {provider_name} API keys for all models are exhausted or excluded."
        )
        return None, None, "all_exhausted"

    async def _resolve_gemini_request(
        self, preferred_model: str, excluded_key_hashes: set[str] | None = None
    ):
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
    ):
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

    async def get_ai_response(
        self,
        api_key: str,
        history: list,
        model_name: str,
        system_instruction: str = None,
        user_id: int = None,
        chat_id: int = None,
        use_openrouter: bool = None,
    ):
        if use_openrouter is None:
            use_openrouter = "/" in model_name or get_use_openrouter()

        if use_openrouter and not get_openrouter_keys():
            return (
                "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                None,
            )

        # Unified path: delegate to Provider classes (same path as ProviderRouter)
        from app.ai_provider import get_provider_for_model

        provider = get_provider_for_model(model_name, api_key)
        response = await provider.get_response(
            history=history,
            model_name=model_name,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
        )

        token_count = response.token_count if response.success else None
        return response.text, token_count

    async def increment_key_usage(
        self, key_hash: str, model_name: str, use_openrouter: bool = None
    ):
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
        system_instruction: str = None,
        user_id: int = None,
        chat_id: int = None,
        use_openrouter: bool = None,
        max_key_retries: int = 3,
    ) -> tuple[str, int | None]:
        """Delegate to ProviderRouter for health-aware key rotation.

        This method exists for backward compatibility. All new code should
        use ProviderRouter.get_response() directly via _get_ai_response_with_routing().
        """
        from app.ai_provider import get_provider_router

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
