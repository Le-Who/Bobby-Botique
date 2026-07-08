"""
AI Provider package — unified interface for Gemini, OpenRouter, Opencode Go, and FreeTheAI.

Re-exports all public symbols for convenient imports:
    from app.providers import AIResponse, GeminiProvider, ProviderRouter, ...
"""

from app.providers.base import (
    AIResponse,
    BaseAIProvider,
    _build_thinking_config,
    get_provider_for_model,
    is_freetheai_model,
    is_opencode_model,
    is_openrouter_model,
)
from app.providers.freetheai import FreeTheAIProvider
from app.providers.gemini import GeminiProvider
from app.providers.opencode import OpencodeGoProvider
from app.providers.openrouter import OpenRouterProvider, close_http_clients
from app.providers.router import ProviderRouter, get_ai_response, get_provider_router

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
