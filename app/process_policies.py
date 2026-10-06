"""Validated process routing plans, resolved once at a request boundary.

Catalog visibility is separate from internal process routing. An explicit plan
never grows hidden fallback models; absent overrides retain legacy behavior.
"""

from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    title: str
    group: str
    setting: str
    fallback: str
    capabilities: tuple[str, ...] = ("text",)
    gemini_only: bool = False
    hedging: bool = False
    connected: bool = True
    note: str = ""


PROCESSES: dict[str, ProcessSpec] = {
    "media.memory_voice": ProcessSpec(
        "Память: голос с метаданными", "Память", "", "gemini-3.1-flash-lite", ("audio_input",), True
    ),
    "media.memory_image": ProcessSpec(
        "Память: описание изображения", "Память", "", "gemini-3.1-flash-lite", ("image",), True
    ),
    "media.memory_document": ProcessSpec(
        "Память: сжатие документа", "Память", "", "gemini-3.1-flash-lite", gemini_only=True
    ),
    "roles.generate": ProcessSpec("Роли: создание инструкции", "Общение", "DEFAULT_MODEL", "gemini-3.6-flash"),
    "roles.edit": ProcessSpec("Роли: улучшение инструкции", "Общение", "DEFAULT_MODEL", "gemini-3.6-flash"),
    "board.synthesis": ProcessSpec("Доска: объединение идей", "Общение", "INLINE_MODEL", "gemini-3.5-flash-lite"),
    "tts.delivery": ProcessSpec(
        "Озвучивание: порядок провайдеров",
        "Аудио",
        "",
        "elevenlabs",
        ("provider_order",),
        note="ID: elevenlabs, gemini. Каждый провайдер генерирует полное сообщение. Можно оставить один.",
    ),
    "asr.delivery": ProcessSpec(
        "Распознавание: порядок провайдеров",
        "Аудио",
        "",
        "pollinations",
        ("provider_order",),
        note="ID: pollinations, gemini. Intent после Whisper сохраняется. Можно оставить один.",
    ),
    "image.pollinations": ProcessSpec(
        "Генерация изображений: Pollinations",
        "Изображения",
        "POLLINATIONS_DEFAULT_IMAGE_MODEL",
        "flux",
        ("image_output",),
        note="Сохраняет Pollen, выбранный формат и параметры улучшения. Доступность ID проверяет провайдер.",
    ),
    "image.fta": ProcessSpec(
        "Генерация / правка изображений: FreeTheAI",
        "Изображения",
        "",
        "vhr/gpt_image_2",
        ("image_output",),
        note="Отдельный endpoint изображений; vhr/… или img/….",
    ),
    "music.fta": ProcessSpec(
        "Музыка: FreeTheAI Lyria",
        "Аудио",
        "",
        "or/google/lyria-3-clip-preview",
        ("music",),
        note="Специализированный исполнитель музыки при выборе модели Lyria.",
    ),
    "asr.pollinations": ProcessSpec(
        "Распознавание речи: Pollinations",
        "Аудио",
        "",
        "whisper",
        ("audio_input",),
        note="Первый этап распознавания перед Gemini; отдельный provider и квота Pollen.",
    ),
    "tts.elevenlabs": ProcessSpec(
        "Озвучивание ответа: ElevenLabs",
        "Аудио",
        "",
        "eleven_multilingual_v2",
        ("audio_output",),
        note="Цепочка полных сообщений; не смешивает части аудио разных моделей.",
    ),
    "image.prompt_extract": ProcessSpec(
        "Изображение: подготовка промпта", "Изображения", "", "gemini-3.1-flash-lite", gemini_only=True
    ),
    "image.translate": ProcessSpec(
        "Изображение: перевод промпта", "Изображения", "", "gemini-3.1-flash-lite", gemini_only=True
    ),
    "trivia.explain": ProcessSpec("Викторина: объяснение ответа", "Игры", "DEFAULT_MODEL", "gemini-3.6-flash"),
    "tarot.chat": ProcessSpec("Таро: диалог", "Астрология", "", "gemini-3.1-flash-lite"),
    "compatibility": ProcessSpec("Совместимость: интерпретация пары", "Астрология", "", "gemini-3.1-flash-lite"),
    "tarot.inline": ProcessSpec("Таро: inline-расклад", "Астрология", "", "gemini-3.1-flash-lite", hedging=True),
    "vision.intent": ProcessSpec(
        "Изображение: определение намерения", "Изображения", "INLINE_MODEL", "gemini-3.1-flash-lite"
    ),
    "vision.search_query": ProcessSpec(
        "Изображение: поисковый запрос", "Изображения", "DEFAULT_MODEL", "gemini-3.6-flash", ("text", "image")
    ),
    "chat": ProcessSpec("Диалог", "Общение", "DEFAULT_MODEL", "gemini-3.6-flash", hedging=True),
    "search": ProcessSpec("Ответ с поиском", "Поиск", "QNA_MODEL", "gemini-3.5-flash-lite"),
    "document": ProcessSpec("Ответ по документу", "Документы", "DEFAULT_MODEL", "gemini-3.6-flash", hedging=True),
    "vision": ProcessSpec(
        "Ответ по изображению / OCR",
        "Изображения",
        "DEFAULT_MODEL",
        "gemini-3.6-flash",
        ("text", "image"),
        hedging=True,
    ),
    "inline": ProcessSpec("Inline-ответ", "Общение", "INLINE_MODEL", "gemini-3.5-flash-lite"),
    "horoscope": ProcessSpec("Гороскоп", "Астрология", "", "gemini-3.5-flash", hedging=True),
    "research": ProcessSpec("Agentic research", "Поиск", "RESEARCH_MODEL", "gemini-3.6-flash", ("text", "tools"), True),
    "summary": ProcessSpec("Сжатие контекста", "Память", "", "gemini-3.1-flash-lite"),
    "memory.extract": ProcessSpec(
        "Извлечение графа памяти", "Память", "", "gemini-3.6-flash", ("json",), True, connected=False
    ),
    "memory.consolidate": ProcessSpec(
        "Консолидация памяти", "Память", "", "gemini-3.5-flash-lite", ("json",), True, connected=False
    ),
    "memory.expand": ProcessSpec(
        "Расширение запроса памяти", "Память", "", "gemini-3.5-flash-lite", gemini_only=True, connected=False
    ),
    "memory.taxonomy": ProcessSpec(
        "Классификация памяти", "Память", "TAXONOMY_MODEL", "gemini-3.5-flash-lite", gemini_only=True, connected=False
    ),
    "embedding": ProcessSpec(
        "Embeddings",
        "Память",
        "",
        "gemini-embedding-2-preview",
        ("embedding",),
        True,
        connected=False,
        note="Смена модели требует переиндексации существующей памяти; отдельная операция миграции.",
    ),
    "tts": ProcessSpec(
        "Озвучивание ответа: Gemini",
        "Аудио",
        "",
        "gemini-3.1-flash-tts-preview",
        ("audio_output",),
        True,
        note="Gemini-цепочка после ElevenLabs, если он настроен.",
    ),
    "asr": ProcessSpec("Распознавание речи: Gemini", "Аудио", "", "gemini-3.1-flash-lite", ("audio_input",), True),
    "image.gemini": ProcessSpec(
        "Генерация изображений: Gemini", "Изображения", "", "gemini-3.1-flash-image", ("image_output",), True
    ),
    "brief.summary": ProcessSpec("Дайджест", "Документы", "", "gemini-3.1-flash-lite", ("json",), True),
    "intent.voice": ProcessSpec(
        "Определение голосового намерения", "Аудио", "OPENCODE_INLINE_MODEL", "opencode-go/big-pickle"
    ),
    "media.intent": ProcessSpec("Намерение после распознавания медиа", "Аудио", "", "gemini-3.1-flash-lite"),
    "memory.relevance": ProcessSpec("Релевантность памяти", "Память", "", "gemini-3.5-flash-lite", ("json",), True),
    "daily.trivia": ProcessSpec("Викторина: вопросы", "Игры", "", "gemini-3.5-flash-lite", ("json",), True),
    "daily.trivia.deduplicate": ProcessSpec(
        "Викторина: дубликаты", "Игры", "", "gemini-3.5-flash-lite", ("json",), True
    ),
    "daily.trivia.audit": ProcessSpec("Викторина: аудит банка", "Игры", "", "gemini-3.5-flash-lite", ("json",), True),
    "natal": ProcessSpec("Натальная интерпретация", "Астрология", "", "gemini-3.6-flash", ("text",), True),
    "tarot.daily": ProcessSpec("Таро дня", "Астрология", "", "gemini-3.5-flash-lite", gemini_only=True),
    "live": ProcessSpec(
        "Live Audio",
        "Аудио",
        "GEMINI_LIVE_MODEL",
        "gemini-3.1-flash-live-preview",
        ("live",),
        True,
        note="Применяется при открытии следующего аудиосеанса.",
    ),
    "live.vertex": ProcessSpec(
        "Live Audio: Vertex",
        "Аудио",
        "",
        "gemini-live-2.5-flash-native-audio",
        ("live",),
        True,
        note="Отдельный Vertex transport; применяется при открытии следующего сеанса.",
    ),
}

for _process in ("extract", "consolidate", "expand", "taxonomy"):
    from dataclasses import replace as _replace

    PROCESSES[f"memory.{_process}"] = _replace(PROCESSES[f"memory.{_process}"], connected=True)

for _process, _title in {
    "words": "Слова",
    "category": "Категория",
    "hints": "Подсказки",
    "judge": "Проверка ответов",
    "image_prompt": "Описание картинки",
}.items():
    PROCESSES[f"crocodile.{_process}"] = ProcessSpec(
        f"Крокодил: {_title}",
        "Игры",
        "DEFAULT_MODEL",
        "gemini-3.6-flash",
        ("json",),
        True,
        note="Auto сохраняет адаптивный маршрут классической игры; ежедневная игра использует DEFAULT_MODEL. Явный план использует Gemini SDK.",
    )


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
    models: tuple[str, ...]
    strategy: str = "legacy"
    explicit: bool = False
    revision: int = 0


def validate_policy(process_id: str, value: Any) -> dict[str, Any]:
    from app.config import is_freetheai_chat_model_id

    spec = PROCESSES[process_id]
    if not spec.connected:
        raise ValueError(spec.note or "Исполнитель ещё не подключён к редактированию политики")
    if not isinstance(value, Mapping) or set(value) - {"models", "strategy", "inherit_user_model"}:
        raise ValueError("Неизвестные поля политики")
    models = value.get("models")
    if not isinstance(models, (list, tuple)) or not 1 <= len(models) <= 12:
        raise ValueError("Укажите от 1 до 12 моделей")
    if process_id in {"live", "live.vertex"} and len(models) != 1:
        raise ValueError("Live использует одну модель на аудиосеанс")
    clean: list[str] = []
    for model in models:
        if not isinstance(model, str) or not 3 <= len(model) <= 200 or any(c.isspace() or ord(c) < 32 for c in model):
            raise ValueError("Некорректный model ID")
        if model in clean:
            raise ValueError("Модели в цепочке не должны повторяться")
        if spec.gemini_only and not model.startswith("gemini-"):
            raise ValueError("Этот исполнитель поддерживает только Gemini")
        if process_id == "media.intent" and not model.startswith(("gemini-", "opencode-go/")):
            raise ValueError("Этот исполнитель поддерживает Gemini и Opencode Go")
        lowered = model.lower()
        if "provider_order" in spec.capabilities:
            allowed = {"elevenlabs", "gemini"} if process_id == "tts.delivery" else {"pollinations", "gemini"}
            if model not in allowed:
                raise ValueError("Неизвестный провайдер аудио")
            clean.append(model)
            continue
        media_prefixes = {
            "image.fta": ("vhr/", "img/"),
            "music.fta": ("or/google/lyria-",),
            "tts.elevenlabs": ("eleven_",),
            "asr.pollinations": ("whisper",),
        }
        if process_id in media_prefixes:
            if not model.startswith(media_prefixes[process_id]):
                raise ValueError("Model ID не соответствует endpoint провайдера")
            clean.append(model)
            continue
        if process_id == "image.pollinations":
            if model.startswith(("gemini-", "imagen-", "vhr/", "img/", "opencode-go/")):
                raise ValueError("Ожидается Pollinations image model ID")
            clean.append(model)
            continue
        required_marker = next(
            (
                marker
                for capability, marker in (("audio_output", "tts"), ("live", "live"), ("image_output", "image"))
                if capability in spec.capabilities
            ),
            None,
        )
        if required_marker and required_marker not in lowered:
            raise ValueError("Model ID не соответствует типу запроса")
        if required_marker is None and not is_freetheai_chat_model_id(model):
            raise ValueError("Эта модель предназначена для другого типа запроса")
        if "embedding" in lowered or (
            required_marker is None and any(token in lowered for token in ("-tts", "-live", "-image"))
        ):
            raise ValueError("Эта модель предназначена для другого типа запроса")
        clean.append(model)
    strategy = value.get("strategy", "sequential")
    if strategy not in {"sequential", "hedged"}:
        raise ValueError("Неизвестная стратегия")
    if strategy == "hedged" and (not spec.hedging or not all(m.startswith("gemini-") for m in clean)):
        raise ValueError("hedged поддерживается только для интерактивных Gemini маршрутов")
    inherit = value.get("inherit_user_model", False)
    if not isinstance(inherit, bool):
        raise ValueError("inherit_user_model должен быть boolean")
    if inherit and "provider_order" in spec.capabilities:
        raise ValueError("Порядок провайдеров не наследует модель пользователя")
    return {"models": clean, "strategy": strategy, "inherit_user_model": inherit}


async def resolve_process(process_id: str, baseline: tuple[str, ...]) -> ResolvedPolicy:
    from app.runtime_settings.lifecycle import operation_snapshot

    if process_id not in PROCESSES:
        raise KeyError(process_id)
    snapshot = await operation_snapshot()
    raw = snapshot.values.get(f"process:{process_id}")
    return resolve_policy_value(process_id, raw, baseline, revision=snapshot.revision)


def resolve_policy_value(
    process_id: str,
    raw: Any,
    baseline: tuple[str, ...],
    *,
    revision: int = 0,
) -> ResolvedPolicy:
    """Pure resolution shared by runtime execution and admin preview."""
    if raw is None:
        return ResolvedPolicy(baseline, revision=revision)
    value = validate_policy(process_id, raw)
    models = tuple(value["models"])
    if value["inherit_user_model"] and baseline:
        models = tuple(dict.fromkeys((baseline[0], *models)))
        validate_policy(process_id, {**value, "models": models})
    return ResolvedPolicy(models, value["strategy"], True, revision)


async def list_processes() -> list[dict[str, Any]]:
    from app.runtime_settings.lifecycle import operation_snapshot
    from app.runtime_settings.process_evidence import process_evidence

    snapshot = await operation_snapshot()
    rows = []
    for process_id, spec in PROCESSES.items():
        baseline = list(await baseline_models(process_id))
        value = snapshot.values.get(f"process:{process_id}")
        rows.append(
            {
                "id": process_id,
                "title": spec.title,
                "group": spec.group,
                "models": list(value["models"]) if isinstance(value, Mapping) else baseline,
                "baseline_models": baseline,
                "source": "admin" if value else "baseline",
                "execution": value.get("strategy", "legacy") if isinstance(value, Mapping) else "legacy",
                "strategy": value.get("strategy", "legacy") if isinstance(value, Mapping) else "legacy",
                "inherit_user_model": value.get("inherit_user_model", False) if isinstance(value, Mapping) else False,
                "capabilities": list(spec.capabilities),
                "strategies": ["sequential", "hedged"] if spec.hedging else ["sequential"],
                "editable": spec.connected,
                "note": spec.note or ("" if spec.connected else "Подключение исполнителя ещё не завершено"),
                "apply": "new_session" if process_id in {"live", "live.vertex"} else "next_request",
                "registry_version": 1,
                **process_evidence(process_id),
            }
        )
    return rows


async def baseline_models(process_id: str) -> tuple[str, ...]:
    """Describe legacy defaults from their actual readers, without applying overrides.

    User-selected models and adaptive routing are request inputs; these defaults
    describe an uncustomized request, rather than pretending to predict its winner.
    """
    from app.config import (
        GEMINI_ECONOMY_MODEL,
        GEMINI_PRIMARY_FALLBACK_MODEL,
        GEMINI_PRIMARY_MODEL,
        get_primary_provider,
        normalize_gemini_runtime_model,
        settings,
    )
    from app.repos.settings_repo import get_global_setting

    spec = PROCESSES[process_id]
    primary = getattr(settings, spec.setting, spec.fallback) if spec.setting else spec.fallback
    if process_id == "tts":
        return (primary, "gemini-2.5-flash-preview-tts")
    if process_id == "tts.elevenlabs":
        return (settings.ELEVENLABS_MODEL,)
    if process_id == "tts.delivery":
        return ("elevenlabs", "gemini")
    if process_id == "asr.delivery":
        return ("pollinations", "gemini")
    if process_id == "asr":
        return (primary, "gemini-3.5-flash")
    if process_id == "media.intent":
        return (primary, "gemini-3.5-flash", "opencode-go/big-pickle")
    if process_id == "search":
        if get_primary_provider() == "opencode":
            return (settings.OPENCODE_QNA_MODEL, settings.QNA_MODEL)
        return (GEMINI_ECONOMY_MODEL, GEMINI_PRIMARY_MODEL)
    if process_id == "daily.trivia":
        chosen = await get_global_setting("daily_trivia_llm_model", GEMINI_ECONOMY_MODEL)
        primary = normalize_gemini_runtime_model(chosen, fallback=GEMINI_ECONOMY_MODEL)
        return tuple(dict.fromkeys((primary, "gemini-3.6-flash", GEMINI_PRIMARY_FALLBACK_MODEL, GEMINI_ECONOMY_MODEL)))
    if process_id.startswith("crocodile."):
        from app.config import is_gemini_chat_model_id
        from app.games.daily_ai import get_daily_text_model
        from app.runtime_settings.legacy_models import read_value

        key = f"daily_croc_text_model_{process_id.partition('.')[2]}"
        selected = await get_global_setting(key, "")
        selected = await read_value(key, selected if is_gemini_chat_model_id(selected) else "")
        chosen = selected or await get_daily_text_model()
        return (chosen,) if chosen else ()
    if process_id == "embedding":
        from app.repos.memory_config import EMBEDDING_MODEL

        return (EMBEDDING_MODEL,)
    if process_id == "summary":
        from app.context.summarizer import SUMMARIZATION_MODEL

        return (SUMMARIZATION_MODEL,)
    if process_id == "board.synthesis":
        from app.handlers.inline import get_inline_model

        return (await get_inline_model(),)
    if process_id == "inline":
        primary = await get_global_setting("inline_model", settings.INLINE_MODEL)
        return tuple(dict.fromkeys((primary, "gemini-3.1-flash-lite")))
    if process_id == "memory.taxonomy":
        from app.repos.memory_config import get_taxonomy_model

        return (get_taxonomy_model(),)
    return (primary,)


async def execute_text_process(
    process_id: str,
    baseline: tuple[str, ...],
    history: list[dict[str, Any]],
    *,
    router: Any = None,
    system_instruction: str | None = None,
    user_id: int | None = None,
    chat_id: int | None = None,
    **legacy_options: Any,
) -> tuple[str, int | None]:
    """Adapt completed-text callers while preserving the unconfigured legacy path."""
    import asyncio
    from dataclasses import replace

    from app.providers import get_provider_router
    from app.providers.request_factory import generation_request_from_history
    from app.providers.stream_types import StreamCompleted, TextDelta, Workload

    router = router or get_provider_router()
    policy = await resolve_process(process_id, baseline)
    if not policy.explicit:
        return await router.get_response(
            preferred_model=baseline[0],
            history=history,
            system_instruction=system_instruction,
            user_id=user_id,
            chat_id=chat_id,
            **legacy_options,
        )
    request = await generation_request_from_history(
        models=policy.models,
        history=history,
        system_instruction=system_instruction,
        user_id=user_id,
        chat_id=chat_id,
        allow_deferred=False,
        workload=Workload.INTERACTIVE,
        thinking_level=legacy_options.get("thinking_level"),
    )
    request = replace(
        request, allow_model_fallback=False, route_strategy=policy.strategy, policy_revision=policy.revision
    )
    chunks: list[str] = []
    async with asyncio.timeout(float(legacy_options.get("timeout") or 120.0)):
        async with aclosing(router.stream(request)) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                elif isinstance(event, StreamCompleted):
                    return "".join(chunks), event.usage.total
                else:
                    raise RuntimeError("Configured model plan did not complete")
    raise RuntimeError("Configured model plan ended without completion")
