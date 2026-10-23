"""
AI Provider package — unified interface for Gemini, OpenRouter, Opencode Go, and FreeTheAI.

Re-exports all public symbols for convenient imports:
    from app.providers import AIResponse, GeminiProvider, ProviderRouter, ...
"""

import asyncio
import logging

from app.providers.base import (
    AIResponse,
    BaseAIProvider,
    _build_thinking_config,
    get_provider_for_model,
    is_freetheai_model,
    is_opencode_model,
    is_openrouter_model,
)
from app.providers.elevenlabs_tts import close_elevenlabs_client
from app.providers.freetheai import FreeTheAIProvider
from app.providers.gemini import GeminiProvider, close_cached_genai_clients
from app.providers.opencode import OpencodeGoProvider
from app.providers.openrouter import OpenRouterProvider
from app.providers.openrouter import close_http_clients as _close_openrouter_http_clients
from app.providers.router import ProviderRouter, get_ai_response, get_provider_router


async def close_http_clients() -> None:
    """Close provider pools independently once their request owners are drained."""
    results = await asyncio.gather(
        _close_openrouter_http_clients(),
        close_cached_genai_clients(),
        close_elevenlabs_client(),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logging.warning("Provider transport cleanup failed")


__all__ = [
    "AIResponse",
    "BaseAIProvider",
    "FreeTheAIProvider",
    "GeminiProvider",
    "OpencodeGoProvider",
    "OpenRouterProvider",
    "ProviderRouter",
    "_build_thinking_config",
    "close_http_clients",
    "get_ai_response",
    "get_provider_for_model",
    "get_provider_router",
    "is_freetheai_model",
    "is_opencode_model",
    "is_openrouter_model",
]
