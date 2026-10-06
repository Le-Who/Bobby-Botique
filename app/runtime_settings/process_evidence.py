"""Auditable executor metadata for the authenticated process editor.

These locations identify runtime consumers, not menu/catalog declarations. They
do not establish provider availability or invalidate already published artifacts.
"""

from typing import TypedDict


class EvidenceLocation(TypedDict):
    file: str
    function: str


class ProcessEvidence(TypedDict):
    executor_family: str
    scope: str
    apply: str
    cache_note: str
    evidence: list[EvidenceLocation]


def _row(
    executor_family: str,
    scope: str,
    cache_note: str,
    *locations: tuple[str, str],
    apply: str = "next_request",
) -> ProcessEvidence:
    return {
        "executor_family": executor_family,
        "scope": scope,
        "apply": apply,
        "cache_note": cache_note,
        "evidence": [{"file": path, "function": function} for path, function in locations],
    }


_CHAT_HISTORY = "Уже отправленные ответы и сохранённая история не пересоздаются."
_MEMORY_GRAPH = "Изменение маршрута не переписывает уже сохранённые факты, связи и согласия памяти."
_MEDIA_MEMORY = "Применяется к новой обработке медиа; уже сохранённые описания и записи памяти не пересоздаются."
_AUDIO = "Применяется при генерации следующей аудиозадачи; уже полученное аудио не перегенерируется."
_TRIVIA_BANK = "Сохранённый банк вопросов и опубликованные викторины не пересоздаются этим контролом."
_CROC_CACHE = (
    "Ключи кэша учитывают соответствующую политику и промпты. "
    "Уже опубликованные ежедневные пазлы автоматически не пересоздаются."
)

PROCESS_EVIDENCE: dict[str, ProcessEvidence] = {
    "media.memory_voice": _row(
        "Gemini SDK: медиа",
        "Голосовые сообщения, обрабатываемые для долговременной памяти с метаданными.",
        _MEDIA_MEMORY,
        ("app/utils/multimodal_processor.py", "_transcribe_voice_for_ltm"),
        ("app/utils/multimodal_processor.py", "_generate_controlled_media"),
    ),
    "media.memory_image": _row(
        "Gemini SDK: медиа",
        "Описание изображения для обработки медиа и памяти.",
        _MEDIA_MEMORY,
        ("app/utils/multimodal_processor.py", "describe_image"),
        ("app/utils/multimodal_processor.py", "_generate_controlled_media"),
    ),
    "media.memory_document": _row(
        "Gemini SDK: текст документа",
        "Сжатие текста документа для обработки медиа и памяти.",
        _MEDIA_MEMORY,
        ("app/utils/multimodal_processor.py", "summarize_document_text"),
        ("app/utils/multimodal_processor.py", "_generate_controlled_media"),
    ),
    "roles.generate": _row(
        "ProviderRouter: завершённый текст",
        "Создание пользовательской инструкции роли, включая повтор создания.",
        "Сохранённая инструкция роли не меняется до нового создания или редактирования.",
        ("app/handlers/msg_roles.py", "handle_custom_role_generation"),
        ("app/handlers/cb_roles.py", "role_custom_retry_callback"),
    ),
    "roles.edit": _row(
        "ProviderRouter: завершённый текст",
        "Улучшение существующей пользовательской инструкции роли.",
        "Новая инструкция сохраняется после результата редактирования.",
        ("app/handlers/msg_roles.py", "handle_edit_prompt"),
    ),
    "board.synthesis": _row(
        "ProviderRouter: inline stream / завершённый текст",
        "Объединение собранных идей доски в итоговый ответ.",
        "Уже полученный результат доски автоматически не пересчитывается.",
        ("app/handlers/board_handler.py", "_run_synthesis"),
    ),
    "tts.delivery": _row(
        "Порядок провайдеров аудио",
        "Выбор последовательности ElevenLabs и Gemini для полного голосового ответа.",
        _AUDIO,
        ("app/voice_engine.py", "VoiceReplyManager._pregenerate_audio"),
    ),
    "asr.delivery": _row(
        "Порядок провайдеров распознавания",
        "Выбор последовательности Pollinations и Gemini при распознавании голоса.",
        "Применяется при следующем распознавании; ранее полученная транскрипция не пересчитывается.",
        ("app/utils/multimodal_processor.py", "transcribe_voice"),
    ),
    "image.pollinations": _row(
        "Pollinations: endpoint изображений",
        "Генерация изображения через Pollinations с его параметрами и квотой Pollen.",
        "Уже отправленные изображения и сохранённые file_id не перегенерируются.",
        ("app/providers/pollinations.py", "PollinationsProvider.generate"),
    ),
    "image.fta": _row(
        "FreeTheAI: endpoint изображений",
        "Генерация и редактирование изображения через специализированный FreeTheAI endpoint.",
        "Уже отправленные изображения и сохранённые file_id не перегенерируются.",
        ("app/providers/freetheai_image.py", "FreeTheAIImageProvider.generate"),
    ),
    "music.fta": _row(
        "FreeTheAI: Lyria",
        "Генерация музыки при выборе модели Lyria.",
        "Изменение цепочки не перегенерирует уже полученные музыкальные файлы.",
        ("app/providers/freetheai_audio.py", "FreeTheAIAudioProvider.generate"),
    ),
    "asr.pollinations": _row(
        "Pollinations: распознавание",
        "Whisper-этап распознавания голоса перед дальнейшей классификацией намерения.",
        "Ранее полученная транскрипция не пересчитывается; действует квота провайдера Pollen.",
        ("app/providers/pollinations.py", "PollinationsProvider.transcribe_audio"),
    ),
    "tts.elevenlabs": _row(
        "ElevenLabs: полный аудиоответ",
        "Цепочка моделей ElevenLabs с ротацией ключей для всех частей ответа.",
        _AUDIO,
        ("app/providers/elevenlabs_tts.py", "generate_speech_with_key_rotation"),
        ("app/voice_engine.py", "VoiceReplyManager._pregenerate_audio"),
    ),
    "image.prompt_extract": _row(
        "Gemini SDK: подготовка промпта",
        "Выделение запроса на рисование из пользовательского текста.",
        "Применяется при следующей подготовке запроса; уже созданное изображение не меняется.",
        ("app/handlers/cmd_image.py", "_extract_draw_prompt_ai"),
    ),
    "image.translate": _row(
        "Gemini SDK: подготовка промпта",
        "Перевод промпта изображения на английский перед генерацией.",
        "Применяется при следующем переводе; уже созданное изображение не меняется.",
        ("app/handlers/cmd_image.py", "_translate_to_english"),
    ),
    "trivia.explain": _row(
        "ProviderRouter: завершённый текст",
        "Объяснение ответа викторины через ссылку запуска бота.",
        _TRIVIA_BANK,
        ("app/handlers/commands.py", "start_command"),
    ),
    "compatibility": _row(
        "ProviderRouter: завершённый текст",
        "Художественная интерпретация локально рассчитанных данных совместимости пары.",
        "Действует для следующего подробного разбора; краткая совместимость по знакам считается локально.",
        ("app/natal/compatibility_interpretation.py", "interpret_compatibility"),
    ),
    "tarot.chat": _row(
        "ProviderRouter: завершённый текст",
        "Продолжение личного диалога по раскладу Таро.",
        _CHAT_HISTORY,
        ("app/handlers/tarot_chat.py", "handle_tarot_message"),
    ),
    "tarot.inline": _row(
        "ProviderRouter: текст расклада",
        "Новая интерпретация выбранного inline-расклада.",
        "Уже опубликованный inline-расклад не пересчитывается этим контролом.",
        ("app/handlers/inline.py", "_generate_tarot_response"),
    ),
    "vision.intent": _row(
        "ProviderRouter: классификация",
        "Определение намерения по описанию пользовательского изображения.",
        "Ранее выбранное намерение и уже обработанное изображение не пересчитываются.",
        ("app/utils/vision_intent.py", "_call_llm_for_intent"),
    ),
    "vision.search_query": _row(
        "ProviderRouter: завершённый текст",
        "Подготовка поискового запроса по изображению или группе изображений.",
        "Действует для следующей подготовки запроса; не повторяет уже выполненный поиск.",
        ("app/handlers/ai_search.py", "_handle_complex_agent_search_leased_impl"),
        ("app/handlers/ai_photo.py", "_handle_complex_media_group_search_leased_impl"),
    ),
    "chat": _row(
        "ProviderRouter: typed stream",
        "Обычный текстовый диалог, включая выбранную пользователем модель при наследовании.",
        _CHAT_HISTORY,
        ("app/handlers/ai_chat.py", "_handle_regular_chat"),
        ("app/handlers/ai_chat.py", "_complete_regular_chat_response"),
    ),
    "search": _row(
        "ProviderRouter: typed stream",
        "Ответ с поисковым контекстом Q&A.",
        "Уже полученные результаты поиска и отправленные ответы автоматически не обновляются.",
        ("app/handlers/ai_search.py", "_handle_qna_search_leased_impl"),
    ),
    "document": _row(
        "ProviderRouter: typed stream",
        "Ответ на вопрос по документу пользователя.",
        _CHAT_HISTORY,
        ("app/handlers/ai_document.py", "_handle_document_question_leased"),
    ),
    "vision": _row(
        "ProviderRouter: typed stream",
        "Ответ по изображению и OCR с входным изображением.",
        _CHAT_HISTORY,
        ("app/handlers/ai_photo.py", "_process_ai_vision"),
    ),
    "inline": _row(
        "ProviderRouter: inline stream",
        "Новый текстовый inline-ответ и его быстрый маршрут.",
        "Сохранённый inline-контекст и уже опубликованные сообщения не пересоздаются.",
        ("app/handlers/inline.py", "_generate_inline_answer"),
        ("app/handlers/inline.py", "_stream_inline_fast"),
    ),
    "horoscope": _row(
        "ProviderRouter: typed stream",
        "Генерация гороскопа через общий обработчик намерения, включая inline-вызов.",
        "Уже отправленный гороскоп автоматически не обновляется.",
        ("app/intent_router.py", "_handle_horoscope"),
        ("app/handlers/inline.py", "_generate_horoscope_inline"),
    ),
    "research": _row(
        "Gemini SDK: agentic research",
        "Исследование с инструментами и финальным синтезом; ключи и резервные модели выбирает обработчик.",
        "Одна операция сохраняет свой снимок настроек; изменение применяется к следующему исследованию.",
        ("app/handlers/ai_search.py", "_handle_research_agent_leased_impl"),
        ("app/core/agentic.py", "AgenticSearch._generate_budgeted"),
    ),
    "summary": _row(
        "ProviderRouter: завершённый текст",
        "Фоновое сжатие истории диалога.",
        "Существующее резюме контекста заменяется только при следующем выполненном сжатии.",
        ("app/context/summarizer.py", "_run_llm_summarization_in_scope"),
    ),
    "memory.extract": _row(
        "Gemini SDK: structured JSON",
        "Извлечение структурированного графа памяти из нового текста.",
        _MEMORY_GRAPH,
        ("app/repos/memory_extraction.py", "extract_graph_structured"),
    ),
    "memory.consolidate": _row(
        "Gemini SDK: structured JSON",
        "Консолидация графа из истории и резервное извлечение фактов.",
        _MEMORY_GRAPH,
        ("app/repos/memory_consolidation.py", "_extract_graph"),
    ),
    "memory.expand": _row(
        "Gemini SDK: расширение запроса",
        "Расширение поискового запроса перед извлечением долговременной памяти.",
        _MEMORY_GRAPH,
        ("app/repos/memory.py", "expand_query_with_llm"),
    ),
    "memory.taxonomy": _row(
        "Gemini SDK: structured JSON",
        "Классификация неоднозначного конфликта при извлечении графа памяти.",
        _MEMORY_GRAPH,
        ("app/repos/memory_extraction.py", "_resolve_ambiguous_conflict"),
    ),
    "embedding": _row(
        "Gemini SDK: embed_content",
        "Фиксированная модель векторизации памяти; изменение через редактор процессов отключено.",
        "Смена модели требует отдельной переиндексации существующих векторов; общий RPD не резервируется.",
        ("app/repos/memory.py", "_get_embedding"),
        apply="manual_migration",
    ),
    "tts": _row(
        "Gemini SDK: TTS",
        "Gemini-цепочка озвучивания полного ответа с AUDIO modality.",
        _AUDIO,
        ("app/voice_engine.py", "VoiceReplyManager._pregenerate_audio"),
        ("app/providers/tts.py", "generate_speech"),
    ),
    "asr": _row(
        "Gemini SDK: распознавание",
        "Gemini-этап распознавания голоса.",
        "Применяется при следующем распознавании; ранее полученная транскрипция не пересчитывается.",
        ("app/utils/multimodal_processor.py", "_transcribe_gemini"),
    ),
    "image.gemini": _row(
        "Gemini SDK: interactions images",
        "Генерация изображения с отдельной квотой IMAGE_GEN_RPD_PER_KEY.",
        "Уже полученные изображения не перегенерируются; общий Gemini RPD не управляет этим endpoint.",
        ("app/providers/imagen_provider.py", "ImagenProvider.generate"),
    ),
    "brief.summary": _row(
        "Gemini SDK: structured JSON",
        "Краткое изложение найденных статей для персонального утреннего обзора.",
        "Уже доставленные обзоры не пересоздаются; результат проходит существующую проверку согласия памяти.",
        ("app/handlers/scheduled_briefs.py", "_generate_brief_summary"),
    ),
    "intent.voice": _row(
        "ProviderRouter: классификация",
        "Классификация неоднозначного запроса на озвучивание.",
        "Применяется при следующей классификации; завершённые голосовые задания не пересчитываются.",
        ("app/voice_intent.py", "_classify_ambiguous_tts_intent"),
    ),
    "media.intent": _row(
        "Gemini SDK / Opencode Go",
        "Определение намерения по тексту после распознавания медиа.",
        "Повторно не распознаёт уже полученную транскрипцию или описание медиа.",
        ("app/utils/multimodal_processor.py", "_classify_intent_with_fallback"),
    ),
    "memory.relevance": _row(
        "Gemini SDK: structured JSON",
        "Проверка релевантности извлечённых воспоминаний текущему запросу.",
        _MEMORY_GRAPH,
        ("app/repos/memory.py", "_search_memories_with_llm_judge_impl"),
    ),
    "daily.trivia": _row(
        "ProviderRouter: Gemini JSON plan",
        "Генерация вопросов ежедневной викторины.",
        _TRIVIA_BANK,
        ("app/games/daily_trivia.py", "generate_question_lane"),
    ),
    "daily.trivia.deduplicate": _row(
        "ProviderRouter: Gemini JSON plan",
        "Семантическая проверка дубликатов при авторинге банка вопросов.",
        _TRIVIA_BANK,
        ("app/games/daily_trivia_authoring.py", "build_semantic_judge"),
    ),
    "daily.trivia.audit": _row(
        "ProviderRouter: Gemini JSON plan",
        "Семантический аудит существующего банка вопросов.",
        "Применяется к следующему запуску аудита; не запускает аудит автоматически.",
        ("app/games/daily_trivia_authoring.py", "audit_semantic_bank"),
    ),
    "natal": _row(
        "ProviderRouter: Gemini model plan / Markdown",
        "Интерпретация рассчитанной натальной карты и проверка качества разделов.",
        "Сохранённые натальные отчёты не перегенерируются этим контролом.",
        ("app/natal/llm.py", "generate_interpretation"),
    ),
    "tarot.daily": _row(
        "ProviderRouter: Gemini model plan",
        "Фоновая подготовка интерпретаций карт дня.",
        "Существующие интерпретации пропускаются без force; изменение применяется к новой подготовке.",
        ("app/tarot_daily.py", "prepare_daily_readings"),
    ),
    "live": _row(
        "Gemini SDK: Live",
        "Новый аудиосеанс через стандартный Live transport.",
        "Уже открытый сеанс сохраняет модель подключения; общий Gemini RPD не резервируется.",
        ("app/web_miniapp.py", "_resolve_live_transport"),
        apply="new_session",
    ),
    "live.vertex": _row(
        "Gemini SDK: Vertex Live",
        "Новый аудиосеанс через отдельный Vertex transport.",
        "Уже открытый сеанс сохраняет модель подключения; используется отдельный Vertex клиент.",
        ("app/web_miniapp.py", "_resolve_live_transport"),
        apply="new_session",
    ),
    "crocodile.words": _row(
        "Крокодил: Gemini SDK / legacy Auto",
        "Генерация банка и отдельных слов классической и ежедневной игры.",
        _CROC_CACHE,
        ("app/games/word_bank.py", "generate_words_for_category"),
        ("app/games/daily_ai.py", "generate_daily_word"),
    ),
    "crocodile.category": _row(
        "Крокодил: Gemini SDK / legacy Auto",
        "Определение категории пользовательского слова.",
        _CROC_CACHE,
        ("app/games/word_bank.py", "resolve_custom_word_category"),
    ),
    "crocodile.hints": _row(
        "Крокодил: Gemini SDK / legacy Auto",
        "Создание и предварительная подготовка подсказок классической и ежедневной игры.",
        _CROC_CACHE,
        ("app/games/hinting.py", "get_or_generate_cached_hints"),
        ("app/games/judge.py", "generate_hints"),
    ),
    "crocodile.judge": _row(
        "Крокодил: Gemini SDK / legacy Auto",
        "Проверка догадок с локальной проверкой и кэшем перед моделью.",
        _CROC_CACHE,
        ("app/games/judge.py", "judge_guess"),
    ),
    "crocodile.image_prompt": _row(
        "Крокодил: Gemini SDK / legacy Auto",
        "Перевод слова и подготовка описания картинки ежедневной игры.",
        _CROC_CACHE,
        ("app/games/crocodile_daily.py", "_translate_word_for_prompt"),
        ("app/games/crocodile_daily.py", "_build_daily_image_prompt"),
    ),
}


def process_evidence(process_id: str) -> ProcessEvidence:
    """Return a fresh JSON-ready row so UI composition cannot alter the map."""
    row = PROCESS_EVIDENCE[process_id]
    return {
        **row,
        "evidence": [{"file": location["file"], "function": location["function"]} for location in row["evidence"]],
    }
