"""
AI Provider abstraction layer for unified API interactions.

This module provides:
- BaseAIProvider abstract class defining common interface
- GeminiProvider and OpenRouterProvider implementations
- Unified get_ai_response() function with automatic provider selection
- Common validation, retry logic, and error handling
"""
import logging
from abc import ABC, abstractmethod
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass

from app.errors import user_friendly_error
from app.resilience_policy import ResiliencePolicy, run_with_resilience


@dataclass
class AIResponse:
    """Standardized response from any AI provider."""
    text: str
    token_count: int
    success: bool
    error_message: Optional[str] = None
    provider: str = ""
    model: str = ""
    
    @property
    def is_error(self) -> bool:
        return not self.success or self.error_message is not None


class BaseAIProvider(ABC):
    """
    Abstract base class for AI providers.
    
    Implements common patterns:
    - Input validation
    - Retry logic with exponential backoff
    - Error categorization
    - Metrics collection
    """
    
    provider_name: str = "base"
    
    def __init__(self, api_key: str):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        self.api_key = api_key
    
    async def get_response(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str] = None,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        max_retries: int = 3,
        timeout: float = 120.0
    ) -> AIResponse:
        """
        Get response from AI provider with retry logic.
        
        Args:
            history: Message history in Gemini format
            model_name: Model identifier
            system_instruction: Optional system prompt
            user_id: User ID for logging
            chat_id: Chat ID for logging  
            max_retries: Maximum retry attempts for transient errors
            timeout: Request timeout in seconds
            
        Returns:
            AIResponse with text and metadata
        """
        # Validate inputs
        self._validate_inputs(history, model_name, user_id, chat_id)
        
        policy = ResiliencePolicy(max_retries=max_retries, timeout_s=timeout)
        last_error = None

        async def _operation() -> AIResponse:
            return await self._execute_request(
                history=history,
                model_name=model_name,
                system_instruction=system_instruction,
                user_id=user_id,
                chat_id=chat_id,
                timeout=timeout,
            )

        try:
            response, _ = await run_with_resilience(
                _operation,
                policy,
                circuit_name=f"ai_provider:{self.provider_name}",
                is_retryable=lambda e: self._is_transient_error(str(e)),
            )
            return response
        except Exception as e:
            last_error = e

        error_msg = user_friendly_error(last_error) if last_error else "Unknown error"
        return AIResponse(
            text=error_msg,
            token_count=0,
            success=False,
            error_message=str(last_error),
            provider=self.provider_name,
            model=model_name
        )
    
    def _validate_inputs(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        user_id: Optional[int],
        chat_id: Optional[int]
    ) -> None:
        """Validate common input parameters."""
        if not isinstance(history, list) or not history:
            raise ValueError("history must be a non-empty list")
        
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        
        if user_id is not None and not isinstance(user_id, int):
            raise ValueError("user_id must be an integer")
        
        if chat_id is not None and not isinstance(chat_id, int):
            raise ValueError("chat_id must be an integer")
    
    def _is_transient_error(self, error_text: str) -> bool:
        """Check if error is transient and can be retried."""
        transient_patterns = [
            "503", "unavailable", "overloaded", "rate limit",
            "timeout", "connection", "temporarily"
        ]
        error_lower = error_text.lower()
        return any(pattern in error_lower for pattern in transient_patterns)
    
    @abstractmethod
    async def _execute_request(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str],
        user_id: Optional[int],
        chat_id: Optional[int],
        timeout: float
    ) -> AIResponse:
        """
        Execute the actual API request. Must be implemented by subclasses.
        """
        pass
    
    def _categorize_error(self, error: Exception) -> str:
        """Categorize error for user-friendly message."""
        error_text = str(error).lower()
        
        if "quota" in error_text:
            return "🚫 Достигнут лимит запросов к API."
        elif "503" in str(error) or "unavailable" in error_text or "overloaded" in error_text:
            return "🔄 Сервер перегружен. Попробуйте ещё раз через несколько секунд."
        elif "rate limit" in error_text:
            return "⏱️ Превышен лимит запросов в секунду. Подождите немного."
        elif "timeout" in error_text:
            return "⏰ Превышено время ожидания. Попробуйте позже."
        elif "invalid" in error_text or "malformed" in error_text:
            return "❌ Некорректный запрос. Проверьте параметры."
        elif "unauthorized" in error_text or "401" in str(error):
            return "🔑 Неверный API ключ."
        elif "402" in str(error):
            return "💳 Недостаточно средств на счету."
        else:
            return f"❌ Произошла ошибка: {error}"


def is_openrouter_model(model_name: str) -> bool:
    """Check if model name indicates an OpenRouter model."""
    return "/" in model_name


def get_provider_for_model(model_name: str, api_key: str) -> BaseAIProvider:
    """
    Factory function to get appropriate provider for a model.
    
    Args:
        model_name: Model identifier
        api_key: API key for the provider
        
    Returns:
        Appropriate AIProvider instance
    """
    # Import here to avoid circular imports
    if is_openrouter_model(model_name):
        # For now, wrap existing function. Full implementation would create OpenRouterProvider
        return _LegacyOpenRouterWrapper(api_key)
    else:
        return _LegacyGeminiWrapper(api_key)


class _LegacyGeminiWrapper(BaseAIProvider):
    """Wrapper for existing get_gemini_response function."""
    
    provider_name = "gemini"
    
    async def _execute_request(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str],
        user_id: Optional[int],
        chat_id: Optional[int],
        timeout: float
    ) -> AIResponse:
        from app.services import get_gemini_response
        
        text, tokens = await get_gemini_response(
            api_key=self.api_key,
            history=history,
            model_name=model_name,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
            max_retries=1  # We handle retries in base class
        )
        
        # Check if response is an error message
        is_error = text.startswith("❌") or text.startswith("🔄") or text.startswith("🚫") or text.startswith("⏰")
        
        return AIResponse(
            text=text,
            token_count=tokens or 0,
            success=not is_error and tokens is not None,
            error_message=text if is_error else None,
            provider=self.provider_name,
            model=model_name
        )


class _LegacyOpenRouterWrapper(BaseAIProvider):
    """Wrapper for existing get_openrouter_response function."""
    
    provider_name = "openrouter"
    
    async def _execute_request(
        self,
        history: List[Dict[str, Any]],
        model_name: str,
        system_instruction: Optional[str],
        user_id: Optional[int],
        chat_id: Optional[int],
        timeout: float
    ) -> AIResponse:
        from app.services import get_openrouter_response
        
        text, tokens = await get_openrouter_response(
            api_key=self.api_key,
            history=history,
            model_name=model_name,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
            max_retries=1  # We handle retries in base class
        )
        
        # Check if response is an error message
        is_error = text.startswith("❌") or text.startswith("🔄") or text.startswith("🚫") or text.startswith("⏰")
        
        return AIResponse(
            text=text,
            token_count=tokens or 0,
            success=not is_error and tokens is not None,
            error_message=text if is_error else None,
            provider=self.provider_name,
            model=model_name
        )


async def get_ai_response(
    api_key: str,
    history: List[Dict[str, Any]],
    model_name: str,
    system_instruction: Optional[str] = None,
    user_id: Optional[int] = None,
    chat_id: Optional[int] = None,
    max_retries: int = 3
) -> Tuple[str, Optional[int]]:
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
    provider = get_provider_for_model(model_name, api_key)
    
    response = await provider.get_response(
        history=history,
        model_name=model_name,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
        max_retries=max_retries
    )
    
    return response.text, response.token_count if response.success else None
