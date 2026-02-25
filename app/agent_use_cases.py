import logging
from typing import List, Optional, Set, Tuple

from app import database as db

from app.config import get_openrouter_keys, get_use_openrouter, settings
from app.errors import is_error_message, is_key_related_error


class AgentRequestUseCase:
    async def resolve_ai_request(
        self,
        preferred_model: str,
        use_openrouter: Optional[bool] = None,
        excluded_key_hashes: Optional[Set[str]] = None,
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
        fallback_priority: List[str],
        excluded_key_hashes: Optional[Set[str]] = None,
        invalidate_cache_func=None,
        provider_name: str = "Unknown",
    ):
        excluded = excluded_key_hashes or set()
        if excluded and invalidate_cache_func:
            await invalidate_cache_func(preferred_model)

        max_attempts = 5
        for _ in range(max_attempts):
            key = await get_key_func(preferred_model)
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
                key = await get_key_func(fallback_model)
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
        self, preferred_model: str, excluded_key_hashes: Optional[Set[str]] = None
    ):
        fallback_priority = [
            settings.RESEARCH_MODEL,
            settings.DEFAULT_MODEL,
            settings.QNA_MODEL,
        ]
        return await self._resolve_key_generic(
            preferred_model,
            db.get_available_gemini_key,
            fallback_priority,
            excluded_key_hashes,
            db.invalidate_key_cache,
            provider_name="Gemini",
        )

    async def _resolve_openrouter_request(
        self, preferred_model: str, excluded_key_hashes: Optional[Set[str]] = None
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
            db.get_available_openrouter_key,
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
            await db.increment_openrouter_key_usage(key_hash, model_name)
        else:
            await db.increment_gemini_key_usage(key_hash, model_name)

    async def get_ai_response_with_key_rotation(
        self,
        preferred_model: str,
        history: list,
        system_instruction: str = None,
        user_id: int = None,
        chat_id: int = None,
        use_openrouter: bool = None,
        max_key_retries: int = 3,
    ) -> Tuple[str, Optional[int]]:
        failed_keys = set()

        for attempt in range(max_key_retries):
            key_data, model_used, resolution = await self.resolve_ai_request(
                preferred_model,
                use_openrouter=use_openrouter,
                excluded_key_hashes=failed_keys,
            )

            if not key_data:
                if resolution == "all_exhausted":
                    is_openrouter = (
                        use_openrouter
                        if use_openrouter is not None
                        else ("/" in preferred_model)
                    )
                    provider_name = "OpenRouter" if is_openrouter else "Gemini"
                    return (
                        f"🚫 Все ключи {provider_name} недоступны или исчерпаны. Попробуйте позже.",
                        None,
                    )
                if resolution == "no_keys":
                    return (
                        "❌ OpenRouter не настроен. Добавьте ключи OpenRouter в настройки.",
                        None,
                    )
                return (
                    "🚫 Не удалось получить доступный ключ API. Попробуйте позже.",
                    None,
                )

            response_text, token_count = await self.get_ai_response(
                key_data["api_key"],
                history,
                model_used,
                system_instruction,
                user_id,
                chat_id,
                use_openrouter,
            )

            if (
                response_text
                and is_error_message(response_text)
                and is_key_related_error(response_text)
            ):
                failed_keys.add(key_data["key_hash"])
                logging.warning(
                    f"Key {key_data['key_hash'][:8]}... failed with key-related error (attempt {attempt + 1}/{max_key_retries}). "
                    f"Error: {response_text[:100]}..."
                )
                continue

            if response_text and not is_error_message(response_text):
                await self.increment_key_usage(
                    key_data["key_hash"], model_used, use_openrouter
                )

            return response_text, token_count

        is_openrouter = (
            use_openrouter if use_openrouter is not None else ("/" in preferred_model)
        )
        provider_name = "OpenRouter" if is_openrouter else "Gemini"
        return (
            f"🚫 Все доступные ключи {provider_name} не сработали ({max_key_retries} попыток). Попробуйте позже.",
            None,
        )
