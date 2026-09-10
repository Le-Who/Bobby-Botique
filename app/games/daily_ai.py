"""Shared Gemini text generation for daily and ordinary Crocodile games.

The historical daily_croc_text_model setting key remains compatible with saved settings.
"""

from __future__ import annotations

import asyncio
import logging
import re

from app.utils.json_compat import json

logger = logging.getLogger(__name__)
DAILY_TEXT_MODEL_SETTING_KEY = "daily_croc_text_model"
TEXT_MODEL_PROCESSES = {
    "words": "Генерация слов",
    "category": "Определение категории",
    "hints": "Подсказки",
    "judge": "Проверка ответов",
    "image_prompt": "Описание для картинки",
}


async def get_daily_text_model() -> str:
    from app.config import is_gemini_chat_model_id
    from app.repos.settings_repo import get_global_setting

    model = (await get_global_setting(DAILY_TEXT_MODEL_SETTING_KEY, "") or "").strip()
    return model if is_gemini_chat_model_id(model) else ""


async def get_daily_text_model_for(process: str) -> str:
    """Resolve a shared daily/classic role, inheriting the historical default."""
    from app.config import is_gemini_chat_model_id
    from app.repos.settings_repo import get_global_setting

    if process not in TEXT_MODEL_PROCESSES:
        raise ValueError(f"Unknown Crocodile text model process: {process}")
    model = (await get_global_setting(f"{DAILY_TEXT_MODEL_SETTING_KEY}_{process}", "") or "").strip()
    return model if is_gemini_chat_model_id(model) else await get_daily_text_model()


async def generate_daily_text(prompt: str, model: str, timeout: float = 30.0) -> str:
    """Call Google GenAI directly, preserving the selected Gemini model and quota accounting."""
    from google.genai import types

    from app.config import is_gemini_chat_model_id, settings
    from app.errors import classify_key_error
    from app.observability.workload_events import observe_workload_call
    from app.providers.gemini import get_cached_genai_client
    from app.repos.keys import get_available_gemini_key, get_key_status_manager, reserve_gemini_key_usage

    model = (model or settings.DEFAULT_MODEL).strip()
    if not is_gemini_chat_model_id(model):
        raise ValueError("Daily text generation requires a Gemini chat model")

    async with asyncio.timeout(timeout):
        key_data = await get_available_gemini_key(model_name=model)
        if not key_data:
            raise RuntimeError("No Gemini key available for daily text generation")
        if not await reserve_gemini_key_usage(key_data["key_hash"], model):
            raise RuntimeError("Gemini quota exhausted for daily text generation")
        client = get_cached_genai_client(key_data["api_key"])
        try:
            response = await observe_workload_call(
                client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json", max_output_tokens=2048),
                ),
                workload="daily_game_generation",
                provider="gemini",
                model=model,
                api_key=key_data["api_key"],
                key_hash=key_data["key_hash"],
                origin="crocodile_daily_ai",
                input_chars=len(prompt),
            )
        except Exception as exc:
            # Reuse the existing cooldown/cache invalidation so the next request
            # can select a healthy key without silently changing the model.
            try:
                await get_key_status_manager().suspend_key(
                    key_data["key_hash"], model, classify_key_error(str(exc)), type(exc).__name__
                )
            except Exception as suspension_error:
                logger.debug("Crocodile key cooldown failed: %s", type(suspension_error).__name__)
            raise
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Gemini returned empty daily text")
        return text.strip()


def _json_object(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    payload = json.loads(cleaned)
    return payload if isinstance(payload, dict) else {}


async def generate_daily_word(topic: str, difficulty: str, used_words: set[str], model: str) -> str | None:
    """Reject malformed or repeated AI words so callers can fall back to the word bank."""
    prompt = (
        f"Игра Крокодил. Придумай одно русское слово для темы {topic}, сложность {difficulty}. "
        "Это должен быть узнаваемый предмет, существо или явление, которое можно изобразить. "
        "Easy: общеизвестное простое слово; hard: менее очевидное, но общеупотребительное. "
        'Ответь только JSON: {"word":"слово"}. Не больше трёх слов. '
        f"Не повторяй ранее использованные: {', '.join(sorted(used_words))}."
    )
    try:
        value = _json_object(await generate_daily_text(prompt, model)).get("word")
        if not isinstance(value, str):
            return None
        word = " ".join(value.lower().split())
        excluded = {" ".join(item.lower().split()).replace("ё", "е") for item in used_words}
        if (
            2 <= len(word) <= 50
            and len(word.split()) <= 3
            and re.fullmatch(r"[а-яё]+(?:[ -][а-яё]+)*", word)
            and word.replace("ё", "е") not in excluded
        ):
            return word
    except Exception as exc:
        logger.warning("Daily word generation failed model=%s: %s", model, type(exc).__name__)
    return None


async def generate_daily_hints(word: str, topic: str, model: str) -> list[str]:
    prompt = (
        f"Игра Крокодил. Секретное слово: {word}. Тема: {topic}. "
        "Дай три разные подсказки на русском, от общей к конкретной. "
        "Не называй само слово и не используй однокоренные слова. "
        'Ответь только JSON: {"hints":["...","...","..."]}.'
    )
    try:
        items = _json_object(await generate_daily_text(prompt, model)).get("hints")
        if not isinstance(items, list) or len(items) != 3:
            return []
        hints = [item.strip() for item in items if isinstance(item, str) and 2 <= len(item.strip()) <= 300]
        if (
            len(hints) == 3
            and len({hint.casefold() for hint in hints}) == 3
            and all(word.casefold().replace("ё", "е") not in hint.casefold().replace("ё", "е") for hint in hints)
        ):
            return hints
    except Exception as exc:
        logger.warning("Daily hint generation failed model=%s: %s", model, type(exc).__name__)
    return []
