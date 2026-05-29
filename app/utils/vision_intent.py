"""Vision intent classification utility."""

import logging
import re
from typing import Literal

from app.config import settings
from app.handlers.ai_core import _get_ai_response_with_routing

# Regex for fast-path detection
# OCR-specific keywords
_OCR_PATTERNS = re.compile(
    r"(перепиши|распознай|что.*написано|какой текст|текст с картинк|"
    r"текст с изображен|перевод текста|скопируй текст|extract text|"
    r"transcribe|read text|copy text)",
    re.IGNORECASE,
)

# Describe-specific keywords to skip LLM entirely
_DESCRIBE_PATTERNS = re.compile(
    r"(опиши|что это|что нарисовано|что изображено|на что похож|"
    r"describe|what is|what's this)",
    re.IGNORECASE,
)

# In-memory dict cache for intent classification
_INTENT_CACHE: dict[str, Literal["ocr", "describe"]] = {}
_MAX_CACHE_SIZE = 128


async def _call_llm_for_intent(caption: str) -> Literal["ocr", "describe"]:
    """Call lightweight model to classify the caption intent."""
    system_instruction = (
        "Ты — классификатор интентов. Проанализируй запрос пользователя к картинке "
        "и определи его намерение (intent).\n"
        "Если пользователь хочет, чтобы с картинки извлекли, переписали, распознали, "
        "скопировали или перевели текст, ответь 'ocr'.\n"
        "Если пользователь хочет, чтобы картинку описали, объяснили, ответили на вопрос "
        "о ней, нашли что-то или проанализировали без прямого извлечения всего текста, ответь 'describe'.\n"
        "Твой ответ должен содержать ровно одно слово: либо 'ocr', либо 'describe'."
    )
    
    # We use settings.INLINE_MODEL (gemini-3.1-flash-lite) for low cost and high speed.
    model = getattr(settings, "INLINE_MODEL", "gemini-3.1-flash-lite")
    
    history = [{"role": "user", "parts": [caption]}]
    try:
        response_text, _ = await _get_ai_response_with_routing(
            preferred_model=model,
            history=history,
            system_instruction=system_instruction,
            timeout=5.0,  # Fast timeout for intent classification
        )
        if response_text:
            cleaned = response_text.strip().lower()
            if "ocr" in cleaned:
                return "ocr"
            if "describe" in cleaned:
                return "describe"
    except Exception as e:
        logging.error("Failed to classify vision intent via LLM: %s", e)
    
    return "describe"


async def classify_vision_intent(caption: str | None) -> Literal["ocr", "describe"]:
    """Classify the user intent for image processing.
    
    Returns:
        "ocr" if the user wants to extract/transcribe text from the image.
        "describe" if the user wants an image description or general analysis.
    """
    if not caption or not caption.strip():
        return "describe"
        
    cleaned = caption.strip()
    if cleaned in _INTENT_CACHE:
        return _INTENT_CACHE[cleaned]
        
    # Manage cache size
    if len(_INTENT_CACHE) >= _MAX_CACHE_SIZE:
        # Simple FIFO eviction
        try:
            first_key = next(iter(_INTENT_CACHE))
            _INTENT_CACHE.pop(first_key, None)
        except StopIteration:
            pass
        
    # 1. Check describe fast path
    if _DESCRIBE_PATTERNS.search(cleaned):
        res: Literal["ocr", "describe"] = "describe"
    # 2. Check OCR fast path
    elif _OCR_PATTERNS.search(cleaned):
        res = "ocr"
    # 3. Fallback to LLM
    else:
        try:
            res = await _call_llm_for_intent(cleaned)
        except Exception as e:
            logging.error("Error classifying vision intent: %s", e)
            res = "describe"
            
    _INTENT_CACHE[cleaned] = res
    return res
