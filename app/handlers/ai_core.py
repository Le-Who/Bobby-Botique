"""
Shared AI orchestration infrastructure.

Contains error handling, key resolution, and AI response wrappers
used by all domain-specific handler modules.
"""

import asyncio
import logging

from telegram import Message

from app.agent_use_cases import AgentRequestUseCase

_agent_use_case = AgentRequestUseCase()


async def handle_ai_response_error(
    response_text: str, placeholder_message: Message, on_error_callback=None
) -> bool:
    """
    Универсальная обработка ошибок AI responseов.
    Убирает дублирование кода обработки ошибок.

    Args:
        response_text: Текст responseа от AI
        placeholder_message: Сообщение for редактирования
        on_error_callback: Опциональный callback for дополнительных действий on ошибке (async функция)

    Returns:
        True if это была ошибка и она была обработана, False if это не ошибка
    """
    from app.errors import (
        build_retry_and_roles_keyboard,
        build_roles_keyboard,
        is_error_message,
        is_retryable_error,
    )

    if not response_text or not is_error_message(response_text):
        return False

    # Execute дополнительные действия before обработкой ошибки (наonмер, очистка истории)
    if on_error_callback:
        try:
            if asyncio.iscoroutinefunction(on_error_callback):
                await on_error_callback()
            else:
                on_error_callback()
        except Exception as e:
            logging.error("Error in on_error_callback: %s", e, exc_info=True)

    # Определяем тип клавиатуры в зависимости от типа ошибки
    if is_retryable_error(response_text):
        reply_markup = build_retry_and_roles_keyboard()
    else:
        reply_markup = build_roles_keyboard()

    # Пытаемся отредактировать message, if не получается - отправляем new
    try:
        await placeholder_message.edit_text(response_text, reply_markup=reply_markup)
    except Exception as edit_error:
        logging.error("Could not edit placeholder message: %s", edit_error)
        try:
            await placeholder_message.reply_text(
                response_text, reply_markup=reply_markup
            )
        except Exception:
            pass

    return True


async def _resolve_ai_request(
    preferred_model: str, use_openrouter: bool = None, excluded_key_hashes: set = None
):
    return await _agent_use_case.resolve_ai_request(
        preferred_model, use_openrouter, excluded_key_hashes
    )


async def _resolve_key_generic(
    preferred_model: str,
    get_key_func,
    fallback_priority: list[str],
    excluded_key_hashes: set = None,
    invalidate_cache_func=None,
    provider_name: str = "Unknown",
):
    return await _agent_use_case._resolve_key_generic(
        preferred_model,
        get_key_func,
        fallback_priority,
        excluded_key_hashes,
        invalidate_cache_func,
        provider_name,
    )


async def _resolve_gemini_request(
    preferred_model: str, excluded_key_hashes: set = None
):
    return await _agent_use_case._resolve_gemini_request(
        preferred_model, excluded_key_hashes
    )


async def _resolve_openrouter_request(
    preferred_model: str, excluded_key_hashes: set = None
):
    return await _agent_use_case._resolve_openrouter_request(
        preferred_model, excluded_key_hashes
    )


async def _get_ai_response(
    api_key: str,
    history: list,
    model_name: str,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
    use_openrouter: bool = None,
):
    return await _agent_use_case.get_ai_response(
        api_key,
        history,
        model_name,
        system_instruction,
        user_id,
        chat_id,
        use_openrouter,
    )


async def _get_ai_response_with_key_rotation(
    preferred_model: str,
    history: list,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
    use_openrouter: bool = None,
    max_key_retries: int = 3,
):
    return await _agent_use_case.get_ai_response_with_key_rotation(
        preferred_model,
        history,
        system_instruction,
        user_id,
        chat_id,
        use_openrouter,
        max_key_retries,
    )


async def _increment_key_usage(
    key_hash: str, model_name: str, use_openrouter: bool = None
):
    await _agent_use_case.increment_key_usage(key_hash, model_name, use_openrouter)


async def _get_ai_response_with_routing(
    preferred_model: str,
    history: list,
    system_instruction: str = None,
    user_id: int = None,
    chat_id: int = None,
    use_openrouter: bool = None,
    max_key_retries: int = 3,
):
    """Health-aware key rotation via ProviderRouter (preferred over plain key rotation)."""
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
