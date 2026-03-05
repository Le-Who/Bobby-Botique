# /app/prompts.py
# Prompt data constants, system instruction composition, and helpers.

from app.prompt_registry import get_registry

# ============================================================================
# ROLE COMPOSITION
# ============================================================================

# Предустановленные роли (минимальный набор для быстрого старта)
DEFAULT_ROLES: dict[str, dict[str, str]] = {
    "teacher": {
        "title": "📚 Преподаватель",
        "prompt": (
            "Ты — Преподаватель. Объясняй по шагам, проверяй понимание, предлагай 2–3 задания.\n"
            "Формат ответа: Краткое резюме → Объяснение → Примеры → Задания → Проверка понимания.\n"
        ),
    },
    "it_engineer": {
        "title": "💻 IT‑инженер",
        "prompt": (
            "Ты — IT‑инженер. Диагностируй проблемы, давай план фикса, предоставляй код‑сниппеты, предупреждай о рисках.\n"
            "Формат ответа: Диагноз → Шаги → Пример кода → Проверки/Валидация → Риски.\n"
        ),
    },
    "doctor_info": {
        "title": "🩺 Доктор (инфо)",
        "prompt": (
            "Ты — медицинский информационный помощник. Даешь образовательную информацию и варианты маршрутизации, без постановки диагнозов.\n"
            "Формат: Кратко по симптомам → Возможные причины (информативно) → Когда обратиться к врачу → Памятка безопасности.\n"
        ),
    },
    "gardener": {
        "title": "🌱 Садовод",
        "prompt": (
            "Ты — Садовод. Давай рекомендации по уходу, сезонные чек‑листы, предупреждай о частых ошибках.\n"
            "Формат: Культура → Условия → Уход → Календарь работ → Типичные ошибки.\n"
        ),
    },
    "lawyer_info": {
        "title": "⚖️ Юрист (инфо)",
        "prompt": (
            "Ты — юридический информационный помощник. Объясняй общие положения и риски, не давая индивидуальных консультаций.\n"
            "Формат: Ситуация → Нормы/практика → Риски → Что подготовить → Куда обратиться.\n"
        ),
    },
    "productivity_coach": {
        "title": "⏱️ Коуч по продуктивности",
        "prompt": (
            "Ты — Коуч по продуктивности. Помогаешь ставить цели, выбирать фреймворки (Pomodoro/Timeboxing), даешь план на сегодня.\n"
            "Формат: Цель → План → Блоки времени → Риски/отвлечения → Ретроспектива.\n"
        ),
    },
}


def compose_system_instruction(role_prompt: str | None, use_compact: bool = True) -> str:
    """Compose the system instruction: base formatting + optional role.

    Delegates to PromptRegistry for thread-safe, LRU-cached composition.

    Args:
        role_prompt: Optional role prompt to append.
        use_compact: If True and role exists, uses compact base to save tokens.

    Returns:
        Composed system prompt string.
    """
    registry = get_registry()
    return registry.compose_system_prompt(role_prompt=role_prompt, use_compact=use_compact)


def clear_prompt_cache():
    """Clear composed prompt caches (useful for testing or settings changes)."""
    registry = get_registry()
    registry.compose_system_prompt.cache_clear()



# ============================================================================
# CUSTOM ROLE CACHE — bounded with TTL
# ============================================================================
from cachetools import TTLCache

_custom_role_cache: TTLCache = TTLCache(maxsize=256, ttl=3600)  # 256 entries, 1h TTL


def get_cached_custom_role(prompt: str) -> dict | None:
    """Получить кастомную роль из кэша по промпту"""
    return _custom_role_cache.get(prompt)


def cache_custom_role(prompt: str, role: dict):
    """Сохранить кастомную роль в кэш (auto-evicts oldest on overflow)."""
    _custom_role_cache[prompt] = role


# ============================================================================
# HELPERS
# ============================================================================
def extract_json_object(text: str) -> dict | None:
    """Извлекает первый валидный JSON-объект из мусорного ответа модели.

    Поддерживает варианты:
    - Ответ в code fence (```json ... ```)
    - Наличие лишнего текста до/после объекта
    - Несколько JSON-структур подряд: берем первый корректный объект с нужными полями
    - Наличие поля 'system_prompt' вместо 'prompt' (конвертируем)
    """
    if not text:
        return None
    cleaned = text.strip()

    # Снимаем внешний code-fence
    if cleaned.startswith("```"):
        lines = cleaned.split("\\n")
        if len(lines) > 1:
            cleaned = "\\n".join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    # Убираем возможные текстовые префиксы типа `json`/`JSON`
    lower = cleaned.lstrip()
    for prefix in ("json\\n", "json\\r\\n", "json ", "JSON\\n", "JSON\\r\\n", "JSON "):
        if lower.startswith(prefix):
            cleaned = cleaned[len(cleaned) - len(lower) + len(prefix) :].lstrip()
            break

    import json

    # Проходим по всем возможным вхождениям '{' и пытаемся собрать сбалансированный объект
    n = len(cleaned)
    for i in range(n):
        if cleaned[i] != "{":
            continue
        depth = 0
        in_string = False
        escape = False
        for j in range(i, n):
            ch = cleaned[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = cleaned[i : j + 1]
                        try:
                            obj = json.loads(candidate)
                        except Exception:
                            break  # текущий блок некорректен, пробуем следующий i
                        if isinstance(obj, dict):
                            # Приводим поле system_prompt -> prompt при необходимости
                            if "prompt" not in obj and "system_prompt" in obj:
                                obj["prompt"] = obj.get("system_prompt")
                            # Проверяем обязательные поля
                            if all(k in obj for k in ("title", "purpose", "prompt")):
                                return obj
                        break
    return None


# ============================================================================
# PROMPT CONSTANTS — MOVED TO prompt_registry.py
# ============================================================================
# All task-specific prompts (QNA_LOCALIZATION, URL_SELECTION, SYNTHESIS,
# IMAGE_ANALYSIS, PROMPT_ENGINEER) are now served from the PromptRegistry.
# Use:  from app.prompt_registry import get_registry
#       get_registry().get_task_prompt("qna_localization", ...)
#       get_registry().get("prompt_engineer").text
# ============================================================================
