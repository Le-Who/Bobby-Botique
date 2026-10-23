# Аудит промптов и TTS от 2026-10-23

Проверены текущие определения, реальные consumers и offline payloads закреплённого Google SDK.
Исходный каталог содержал 94 ID, включая семь исторических шаблонов без активного читателя.
После регистрации четырёх прежних literals контекста и общего `tarot.scope` — 99 ID.
Это проверка contracts и конфигурации, а не доказательство качества живой генерации или произношения.

Каждый текст прочитан заново: задача, источники данных, placeholders, язык, структура JSON/XML/Markdown,
совпадение с парсером, граница цитат/документов/памяти, фактическая длина и допустимые параметры.
Просмотрены прямые `GenerateContentConfig`, `system_instruction`, динамические wrappers и TTS consumers.
Старые audit notes не использовались как доказательство реализации.

## Документация и исправления

Официальные документы получены 2026-10-23 через Google Developer Knowledge `search_documents` → `get_documents`:

- [Gemini 3.6 / 3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.6):
  sampling deprecated и ignored начиная с этих выпусков; в будущих поколениях возможен 400.
  Последний непустой model turn запрещён. Текущие task flows заканчиваются пользовательским запросом.
- [Рекомендации Gemini 3.x](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5):
  default sampling рекомендуется всем Gemini 3.x. Текстовый builder удаляет `temperature/top_p/top_k/candidate_count`;
  для Gemini 2.5 прежние значения сохраняются. Модели и env/admin precedence не менялись.
- [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) и
  [thinking GenerateContent](https://ai.google.dev/gemini-api/docs/generate-content/thinking):
  off отображается в low для 3.8 и Pro 3.x; medium → high для Gemini 3 Pro.
  У 2.5 Pro минимальный budget 128; off для 2.5 Flash по-прежнему 0.
  В 3.8 `max_output_tokens` включает thinking: только tiny consumers с cap меньше 1024 без явной thinking-политики
  получают low и общий cap 1024. Большие caps, другие модели и явные preferences сохраняются.
- [Speech generation](https://ai.google.dev/gemini-api/docs/speech-generation):
  transcript отделён от краткого `speech_metadata.style`; длинные director/persona blocks повышают риск voice drift.
  У всех восьми штатных голосов Gemini раньше были пустые индивидуальные prompts. Добавлены короткие directions;
  удалены flat/rigid/extremely fast указания. Сохранены raw 24 kHz PCM, `store=False` и полная валидация audio.
- [Function-call matching](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5):
  research теперь возвращает исходные id/name/count/order, включая локально отклонённые вызовы.
  Исходные model parts и thought signatures сохраняются.

Текстовые изменения потребовались общему языковому контракту, native search, trivia explanation,
части Crocodile, visual intent, Live, TTS, Tarot и инструкциям контекста. Остальные baselines сохранены после проверки.
Production overrides, персональные копии ролей и уже опубликованные материалы не перезаписывались.

Baseline сжатия истории фактически остаётся `gemini-3.1-flash-lite`: constant в
[token_budget.py](../app/context/token_budget.py), reader в [summarizer.py](../app/context/summarizer.py),
special-case resolver в [process_policies.py](../app/process_policies.py). Это активный маршрут с admin override.

## Полный inventory

Источник — место определения. Literal readers получены source index; динамические preset roles,
общие фрагменты и Tarot проверены вручную. Пустой literal index не доказывает отсутствие использования.

| ID | Источник | Читатель / применение | Решение |
| --- | --- | --- | --- |
| `system_prompt_full` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/prompt_registry.py:PromptRegistry._compose / dynamic reader](../app/prompt_registry.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `system_prompt_compact` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/prompt_registry.py:PromptRegistry._compose / dynamic reader](../app/prompt_registry.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `qna_localization` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `search.native`. |
| `url_selection` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `research_agent_system`. |
| `synthesis` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `research.synthesis.system`. |
| `image_analysis` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/ai_photo.py:_handle_complex_media_group_search_leased_impl](../app/handlers/ai_photo.py); [app/handlers/ai_search.py:_handle_complex_agent_search_leased_impl](../app/handlers/ai_search.py) | Проверены single/group cardinality, видимые факты, OCR verbatim/перевод и неразборчивый текст. |
| `prompt_engineer` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py:role_custom_retry_callback](../app/handlers/cb_roles.py); [app/handlers/msg_roles.py:handle_custom_role_generation](../app/handlers/msg_roles.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `summarization_system` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `summary.system`. |
| `summarization_chunk` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `summary.chunk`. |
| `research_agent_system` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/core/agentic.py:AgenticSearch._get_system_instruction](../app/core/agentic.py) | Проверены tool results/URL и budgets как пределы; исправлены sampling и FunctionResponse matching. |
| `formatting_rules` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/prompt_registry.py:PromptRegistry._compose / dynamic reader](../app/prompt_registry.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `formatting_rules_compact` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/ai_document.py:_handle_document_question_leased](../app/handlers/ai_document.py); [app/handlers/ai_photo.py:_build_vision_prompt](../app/handlers/ai_photo.py); [app/handlers/inline.py:_generate_and_edit_inline](../app/handlers/inline.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `voice_tag_instruction` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/prompt_registry.py:PromptRegistry._compose / dynamic reader](../app/prompt_registry.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `intent_routing_instruction` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/prompt_registry.py:PromptRegistry._compose / dynamic reader](../app/prompt_registry.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `smart_suggestions_instruction` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/prompt_registry.py:PromptRegistry._compose / dynamic reader](../app/prompt_registry.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `summarization_refine_first` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `summary.first`. |
| `summarization_refine_subsequent` | [app/prompt_registry.py](../app/prompt_registry.py) | Исторический, активного вызова нет | Исторический, без текущего читателя; сохранён для совместимости. Замена: `summary.refine`. |
| `role.teacher` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py](../app/handlers/cb_roles.py) → сохранённая роль → `PromptRegistry._compose` | Проверен профиль вместе с общим factuality/language/format контрактом; применяется при новом выборе роли. |
| `role.it_engineer` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py](../app/handlers/cb_roles.py) → сохранённая роль → `PromptRegistry._compose` | Проверен профиль вместе с общим factuality/language/format контрактом; применяется при новом выборе роли. |
| `role.doctor_info` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py](../app/handlers/cb_roles.py) → сохранённая роль → `PromptRegistry._compose` | Проверен профиль вместе с общим factuality/language/format контрактом; применяется при новом выборе роли. |
| `role.gardener` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py](../app/handlers/cb_roles.py) → сохранённая роль → `PromptRegistry._compose` | Проверен профиль вместе с общим factuality/language/format контрактом; применяется при новом выборе роли. |
| `role.lawyer_info` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py](../app/handlers/cb_roles.py) → сохранённая роль → `PromptRegistry._compose` | Проверен профиль вместе с общим factuality/language/format контрактом; применяется при новом выборе роли. |
| `role.productivity_coach` | [app/prompt_registry.py](../app/prompt_registry.py) | [app/handlers/cb_roles.py](../app/handlers/cb_roles.py) → сохранённая роль → `PromptRegistry._compose` | Проверен профиль вместе с общим factuality/language/format контрактом; применяется при новом выборе роли. |
| `summary.system` | [app/context/summarizer.py](../app/context/summarizer.py) | [app/context/summarizer.py:_run_llm_summarization_in_scope](../app/context/summarizer.py) | Проверены полный refine replacement, один проход подстановки, различение планов/результатов; budget/consent у consumer. |
| `summary.chunk` | [app/context/summarizer.py](../app/context/summarizer.py) | [app/context/summarizer.py:_run_llm_summarization_in_scope](../app/context/summarizer.py) | Проверены полный refine replacement, один проход подстановки, различение планов/результатов; budget/consent у consumer. |
| `summary.first` | [app/context/summarizer.py](../app/context/summarizer.py) | [app/context/summarizer.py:_run_llm_summarization_in_scope](../app/context/summarizer.py) | Проверены полный refine replacement, один проход подстановки, различение планов/результатов; budget/consent у consumer. |
| `summary.refine` | [app/context/summarizer.py](../app/context/summarizer.py) | [app/context/summarizer.py:_run_llm_summarization_in_scope](../app/context/summarizer.py) | Проверены полный refine replacement, один проход подстановки, различение планов/результатов; budget/consent у consumer. |
| `context.summary.preamble` | [app/context/assembler.py](../app/context/assembler.py) | [app/context/assembler.py:ContextAssembler._summary_token_cost](../app/context/assembler.py); [app/context/assembler.py:ContextAssembler._build_final_history](../app/context/assembler.py) | Ранее literal, теперь controlled: доверие/даты/summary. Стоимость рамки включена в реальный input budget. |
| `context.summary.ack` | [app/context/assembler.py](../app/context/assembler.py) | [app/context/assembler.py:ContextAssembler._summary_token_cost](../app/context/assembler.py); [app/context/assembler.py:ContextAssembler._build_final_history](../app/context/assembler.py) | Ранее literal, теперь controlled: доверие/даты/summary. Стоимость рамки включена в реальный input budget. |
| `context.memory_safety` | [app/context/compression.py](../app/context/compression.py) | [app/context/compression.py:format_compressed_context](../app/context/compression.py) | Ранее literal, теперь controlled: доверие/даты/summary. Стоимость рамки включена в реальный input budget. |
| `context.temporal` | [app/context/compression.py](../app/context/compression.py) | [app/context/compression.py:inject_memory_layers](../app/context/compression.py) | Ранее literal, теперь controlled: доверие/даты/summary. Стоимость рамки включена в реальный input budget. |
| `crocodile.image_prompt` | [app/games/crocodile_daily.py](../app/games/crocodile_daily.py) | [app/games/crocodile_daily.py:_translate_word_for_prompt](../app/games/crocodile_daily.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `crocodile.word` | [app/games/daily_ai.py](../app/games/daily_ai.py) | [app/games/daily_ai.py:generate_daily_word](../app/games/daily_ai.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `crocodile.hints` | [app/games/daily_ai.py](../app/games/daily_ai.py) | [app/games/daily_ai.py:generate_daily_hints](../app/games/daily_ai.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `trivia.main` | [app/games/daily_trivia.py](../app/games/daily_trivia.py) | [app/games/daily_trivia.py:generate_question_lane](../app/games/daily_trivia.py) | Проверены количество, JSON, индексы, identity.answer и boolean с реальным parser/bank validation. |
| `trivia.super` | [app/games/daily_trivia.py](../app/games/daily_trivia.py) | [app/games/daily_trivia.py:generate_question_lane](../app/games/daily_trivia.py) | Проверены количество, JSON, индексы, identity.answer и boolean с реальным parser/bank validation. |
| `trivia.deduplicate` | [app/games/daily_trivia_authoring.py](../app/games/daily_trivia_authoring.py) | [app/games/daily_trivia_authoring.py:build_semantic_judge.judge](../app/games/daily_trivia_authoring.py) | Проверены количество, JSON, индексы, identity.answer и boolean с реальным parser/bank validation. |
| `trivia.audit` | [app/games/daily_trivia_authoring.py](../app/games/daily_trivia_authoring.py) | [app/games/daily_trivia_authoring.py:audit_semantic_bank](../app/games/daily_trivia_authoring.py) | Проверены количество, JSON, индексы, identity.answer и boolean с реальным parser/bank validation. |
| `crocodile.hints.batch` | [app/games/hinting.py](../app/games/hinting.py) | [app/games/hinting.py:_generate_batched_hints](../app/games/hinting.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `crocodile.judge.classic` | [app/games/judge.py](../app/games/judge.py) | [app/games/judge.py:_race_generate](../app/games/judge.py); [app/games/judge.py:_generate_judgement](../app/games/judge.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `crocodile.hints.classic` | [app/games/judge.py](../app/games/judge.py) | [app/games/judge.py:generate_hints](../app/games/judge.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `crocodile.category` | [app/games/word_bank.py](../app/games/word_bank.py) | [app/games/word_bank.py:resolve_custom_word_category](../app/games/word_bank.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `crocodile.words.bank` | [app/games/word_bank.py](../app/games/word_bank.py) | [app/games/word_bank.py:generate_words_for_category](../app/games/word_bank.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `crocodile.words.fast` | [app/games/word_bank.py](../app/games/word_bank.py) | [app/games/word_bank.py:_generate_single_word_fast](../app/games/word_bank.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `document.answer` | [app/handlers/ai_document.py](../app/handlers/ai_document.py) | [app/handlers/ai_document.py:_handle_document_question_leased](../app/handlers/ai_document.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `tarot.scope` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py); [app/tarot_daily.py:_build_daily_system_instruction](../app/tarot_daily.py) | Новый общий символический контракт: переданные карты/ориентация, без вымышленных событий/чужих намерений. |
| `research.synthesis.system` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/core/agentic.py:AgenticSearch._run](../app/core/agentic.py) | Проверены tool results/URL и budgets как пределы; исправлены sampling и FunctionResponse matching. |
| `research.synthesis.request` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/core/agentic.py:AgenticSearch._run](../app/core/agentic.py) | Проверены tool results/URL и budgets как пределы; исправлены sampling и FunctionResponse matching. |
| `tarot.inline.classic` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `tarot.inline.daily` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `tarot.inline.yes_no` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `tarot.inline.love` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `tarot.inline.celtic` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `tarot.inline.fallback` | [app/runtime_settings/interactive_prompts.py](../app/runtime_settings/interactive_prompts.py) | [app/runtime_settings/interactive_prompts.py:build_tarot_prompt](../app/runtime_settings/interactive_prompts.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `search.native` | [app/handlers/ai_search.py](../app/handlers/ai_search.py) | [app/handlers/ai_search.py:_handle_qna_search_leased_impl](../app/handlers/ai_search.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `vision.intent` | [app/utils/vision_intent.py](../app/utils/vision_intent.py) | [app/utils/vision_intent.py:_call_llm_for_intent](../app/utils/vision_intent.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `vision.search_group` | [app/handlers/ai_photo.py](../app/handlers/ai_photo.py) | [app/handlers/ai_photo.py:_handle_complex_media_group_search_leased_impl](../app/handlers/ai_photo.py) | Проверены single/group cardinality, видимые факты, OCR verbatim/перевод и неразборчивый текст. |
| `vision.describe` | [app/handlers/ai_photo.py](../app/handlers/ai_photo.py) | [app/handlers/ai_photo.py:_build_vision_prompt](../app/handlers/ai_photo.py) | Проверены single/group cardinality, видимые факты, OCR verbatim/перевод и неразборчивый текст. |
| `vision.describe_group` | [app/handlers/ai_photo.py](../app/handlers/ai_photo.py) | [app/handlers/ai_photo.py:_build_vision_prompt](../app/handlers/ai_photo.py) | Проверены single/group cardinality, видимые факты, OCR verbatim/перевод и неразборчивый текст. |
| `vision.ocr` | [app/handlers/ai_photo.py](../app/handlers/ai_photo.py) | [app/handlers/ai_photo.py:_build_ocr_prompt](../app/handlers/ai_photo.py) | Проверены single/group cardinality, видимые факты, OCR verbatim/перевод и неразборчивый текст. |
| `board.synthesis` | [app/handlers/board_handler.py](../app/handlers/board_handler.py) | [app/handlers/board_handler.py:_run_synthesis](../app/handlers/board_handler.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `inline.system` | [app/handlers/inline.py](../app/handlers/inline.py) | [app/handlers/inline.py:_generate_and_edit_inline](../app/handlers/inline.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `trivia.explain` | [app/handlers/commands.py](../app/handlers/commands.py) | [app/handlers/commands.py:start_command](../app/handlers/commands.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `roles.edit` | [app/handlers/msg_roles.py](../app/handlers/msg_roles.py) | [app/handlers/msg_roles.py:handle_edit_prompt](../app/handlers/msg_roles.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `brief.summary` | [app/handlers/scheduled_briefs.py](../app/handlers/scheduled_briefs.py) | [app/handlers/scheduled_briefs.py:_generate_brief_summary](../app/handlers/scheduled_briefs.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `compatibility.interpretation` | [app/natal/compatibility_interpretation.py](../app/natal/compatibility_interpretation.py) | [app/natal/compatibility_interpretation.py:interpret_compatibility](../app/natal/compatibility_interpretation.py) | Проверены расчётные данные, символический характер, неопределённость времени/домов, стабильные секции и язык. |
| `natal.interpretation` | [app/natal/llm.py](../app/natal/llm.py) | [app/natal/llm.py:build_interpretation_prompt](../app/natal/llm.py) | Проверены расчётные данные, символический характер, неопределённость времени/домов, стабильные секции и язык. |
| `natal.repair` | [app/natal/llm.py](../app/natal/llm.py) | [app/natal/llm.py:_build_interpretation_repair_prompt](../app/natal/llm.py) | Проверены расчётные данные, символический характер, неопределённость времени/домов, стабильные секции и язык. |
| `tts.director` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_steerable_tts_prompt](../app/providers/tts.py) | Только legacy GenerateContent; исключён из Gemini 3.8 metadata, уточнены неспроизносимые directions. |
| `tts.style.neutral` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_delivery_direction](../app/providers/tts.py) | Краткая естественная подача/темп; удалены flat/rigid/extremely fast указания; дополнено профилем голоса. |
| `tts.style.expressive` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_delivery_direction](../app/providers/tts.py) | Краткая естественная подача/темп; удалены flat/rigid/extremely fast указания; дополнено профилем голоса. |
| `tts.style.conversational` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_delivery_direction](../app/providers/tts.py) | Краткая естественная подача/темп; удалены flat/rigid/extremely fast указания; дополнено профилем голоса. |
| `tts.pacing.neutral` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_delivery_direction](../app/providers/tts.py) | Краткая естественная подача/темп; удалены flat/rigid/extremely fast указания; дополнено профилем голоса. |
| `tts.pacing.expressive` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_delivery_direction](../app/providers/tts.py) | Краткая естественная подача/темп; удалены flat/rigid/extremely fast указания; дополнено профилем голоса. |
| `tts.pacing.conversational` | [app/providers/tts.py](../app/providers/tts.py) | [app/providers/tts.py:_get_delivery_direction](../app/providers/tts.py) | Краткая естественная подача/темп; удалены flat/rigid/extremely fast указания; дополнено профилем голоса. |
| `memory.expand` | [app/repos/memory.py](../app/repos/memory.py) | [app/repos/memory.py:expand_query_with_llm](../app/repos/memory.py) | Проверены атрибуция, отрицание, время, JSON и provenance с парсером; исправлены SDK generation settings. |
| `memory.relevance` | [app/repos/memory.py](../app/repos/memory.py) | [app/repos/memory.py:_search_memories_with_llm_judge_impl](../app/repos/memory.py) | Проверены атрибуция, отрицание, время, JSON и provenance с парсером; исправлены SDK generation settings. |
| `memory.consolidate` | [app/repos/memory_consolidation.py](../app/repos/memory_consolidation.py) | [app/repos/memory_consolidation.py:_extract_graph](../app/repos/memory_consolidation.py) | Проверены атрибуция, отрицание, время, JSON и provenance с парсером; исправлены SDK generation settings. |
| `memory.consolidate.fallback` | [app/repos/memory_consolidation.py](../app/repos/memory_consolidation.py) | [app/repos/memory_consolidation.py:_extract_graph.execute_override](../app/repos/memory_consolidation.py); [app/repos/memory_consolidation.py:_extract_graph](../app/repos/memory_consolidation.py) | Проверены атрибуция, отрицание, время, JSON и provenance с парсером; исправлены SDK generation settings. |
| `memory.extract` | [app/repos/memory_extraction.py](../app/repos/memory_extraction.py) | [app/repos/memory_extraction.py:extract_graph_structured](../app/repos/memory_extraction.py) | Проверены атрибуция, отрицание, время, JSON и provenance с парсером; исправлены SDK generation settings. |
| `memory.taxonomy` | [app/repos/memory_extraction.py](../app/repos/memory_extraction.py) | [app/repos/memory_extraction.py:_resolve_ambiguous_conflict](../app/repos/memory_extraction.py) | Проверены атрибуция, отрицание, время, JSON и provenance с парсером; исправлены SDK generation settings. |
| `horoscope.system` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/intent_router.py:_build_horoscope_system_instruction](../app/intent_router.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `crocodile.image.scene` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/games/crocodile_daily.py:_build_daily_image_prompt](../app/games/crocodile_daily.py) | Проверены JSON/количество, подсказки без раскрытия слова, тема и ограничения изображения с consumer. |
| `inline.tabs` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/handlers/inline.py:_generate_and_edit_inline](../app/handlers/inline.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `inline.search.enabled` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/handlers/inline.py:_generate_and_edit_inline](../app/handlers/inline.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `inline.search.disabled` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/handlers/inline.py:_generate_and_edit_inline](../app/handlers/inline.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `chat.continue` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/handlers/cb_ai_actions.py:continue_stream_callback](../app/handlers/cb_ai_actions.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `media.intent.text` | [app/runtime_settings/additional_prompts.py](../app/runtime_settings/additional_prompts.py) | [app/utils/multimodal_processor.py:_transcribe_pollinations](../app/utils/multimodal_processor.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |
| `live.default` | [app/runtime_settings/media_prompts.py](../app/runtime_settings/media_prompts.py) | [app/web_miniapp.py:_build_live_system_instruction](../app/web_miniapp.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `live.vertex.search` | [app/runtime_settings/media_prompts.py](../app/runtime_settings/media_prompts.py) | [app/web_miniapp.py:_build_live_system_instruction](../app/web_miniapp.py) | Уточнены язык/источники/неопределённость/task-data boundary; placeholders и выходной формат сохранены. |
| `image.prompt_extract` | [app/runtime_settings/media_prompts.py](../app/runtime_settings/media_prompts.py) | [app/handlers/cmd_image.py:_extract_draw_prompt_ai.execute](../app/handlers/cmd_image.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `image.translate` | [app/runtime_settings/media_prompts.py](../app/runtime_settings/media_prompts.py) | [app/handlers/cmd_image.py:_translate_to_english.execute](../app/handlers/cmd_image.py) | Проверены placeholders, формат consumer, входные данные и factuality; изменение текста не требуется. |
| `tarot.daily` | [app/tarot_daily.py](../app/tarot_daily.py) | [app/tarot_daily.py:_build_daily_system_instruction](../app/tarot_daily.py) | Формат сохранён; consumer добавляет общий tarot.scope, без изменения розыгрыша карт. |
| `media.asr` | [app/utils/multimodal_processor.py](../app/utils/multimodal_processor.py) | [app/utils/multimodal_processor.py:_transcribe_pollinations](../app/utils/multimodal_processor.py); [app/utils/multimodal_processor.py:_transcribe_gemini_plan](../app/utils/multimodal_processor.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |
| `media.image_description` | [app/utils/multimodal_processor.py](../app/utils/multimodal_processor.py) | [app/utils/multimodal_processor.py:describe_image](../app/utils/multimodal_processor.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |
| `media.document_summary` | [app/utils/multimodal_processor.py](../app/utils/multimodal_processor.py) | [app/utils/multimodal_processor.py:summarize_document_text](../app/utils/multimodal_processor.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |
| `media.voice_memory` | [app/utils/multimodal_processor.py](../app/utils/multimodal_processor.py) | [app/utils/multimodal_processor.py:_transcribe_voice_for_ltm](../app/utils/multimodal_processor.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |
| `intent.voice` | [app/voice_intent.py](../app/voice_intent.py) | [app/voice_intent.py:_build_classifier_prompt](../app/voice_intent.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |
| `intent.voice.system` | [app/voice_intent.py](../app/voice_intent.py) | [app/voice_intent.py:_classify_ambiguous_tts_intent](../app/voice_intent.py) | Проверены транскрипция/классификация, язык, служебные метки и различение цитат/намерения пользователя. |

## Голоса и параметры

Проверены все восемь голосов Gemini и семь штатных голосов ElevenLabs в [audio.py](../app/runtime_settings/audio.py), [Gemini TTS](../app/providers/tts.py), [ElevenLabs TTS](../app/providers/elevenlabs_tts.py) и [voice engine](../app/voice_engine.py). Это каталог приложения; доступ проекта к нему не проверялся, все 30 prebuilt Google voices автоматически не добавлялись.

Приоритет: личная tts_temperature → optional voice.temperature → общая temperature. В 3.8 это уровень текстовой подачи/темпа, sampling API не отправляется. Legacy GenerateContent также получает temperature с существующим минимумом 0.5. Числа — консервативные стартовые настройки, не измеренные оптимумы.

| Gemini voice | Краткая инструкция | Уровень / legacy temperature |
| --- | --- | --- |
| Aoede | Breezy, relaxed conversation, with gentle emphasis and natural pauses. | 0.6 |
| Kore | Firm, assured delivery, with clear diction and measured emphasis. | 0.55 |
| Puck | Upbeat, friendly conversation, with lively inflection and an easy rhythm. | 0.75 |
| Charon | Informative, composed narration, with precise articulation and unhurried phrasing. | 0.5 |
| Leda | Light, bright conversation, with gentle energy and a flowing rhythm. | 0.65 |
| Orus | Firm, grounded narration, with deliberate phrasing and restrained emphasis. | 0.5 |
| Zephyr | Bright, cheerful delivery, with crisp diction and a buoyant, comfortable rhythm. | 0.7 |
| Rasalgethi | Engaging, informative narration, with clear phrasing and emphasis on key ideas. | 0.55 |

Текущий адаптер ElevenLabs eleven_multilingual_v2 озвучивает text без отдельного director prompt/temperature. Инструкция внутри transcript изменила бы произносимое содержимое. Общий baseline: stability 0.5, similarity_boost 0.8, style 0.25, use_speaker_boost true, normalization on.

| ElevenLabs voice | ID | Prompt / temperature в текущем адаптере |
| --- | --- | --- |
| Charlotte (разговорный) | `XB0fDUnXU5powFXDhCwa` | Не передаются; общий voice_settings |
| Rachel (спокойный) | `21m00Tcm4TlvDq8ikWAM` | Не передаются; общий voice_settings |
| Adam (глубокий) | `pNInz6obpgDQGcFmaJgB` | Не передаются; общий voice_settings |
| Antoni (дружелюбный) | `ErXwobaYiN019PkySvjV` | Не передаются; общий voice_settings |
| Brian (профессиональный) | `nPczCjzI2devNBz1zQrb` | Не передаются; общий voice_settings |
| Liam (энергичный) | `TX3LPaxmHKxFdv7VOQHJ` | Не передаются; общий voice_settings |
| Bella (мягкий) | `EXAVITQu4vr4xnSDxMaL` | Не передаются; общий voice_settings |


## Доказательства и ограничения

Проверки используют реальные types и сериализацию закреплённого SDK с локальным MockTransport.
Внешние инструменты/БД заменены offline fixtures. Покрыты omission sampling, поддерживаемые thinking levels,
3.8 tiny-budget policy и explicit-policy precedence, id/order/thought signatures, каждый штатный Gemini voice,
старые audio snapshots, user/voice/global priority, один проход подстановки, summary wrapper cost и UTF-8 memory budget.

Смежная wire-проверка подтверждает native Gemini typed outcomes без текста ошибки:
ReadTimeout → TIMEOUT, API 429 → RATE_LIMIT, 504 → TIMEOUT, 401 → INVALID_KEY.
Точные финальные общие gates фиксируются в общем отчёте для неизменяемого итогового дерева.

Также прочитаны не являющиеся editable IDs tool descriptions research (search_web/read_page/conclude_research/recall_memory),
короткий degraded fallback AgenticSearch._get_system_instruction, XML-поля памяти, task-data/date wrappers
и копии пользовательских ролей. Ранее встроенные управляющие memory/temporal/summary literals вынесены в каталог.
Наличие старой записи не объявляет текущий факт или новое событие; temporal instruction больше не предписывает
поздравлять или сочувствовать без подтверждения даты и релевантности.

Production overrides, права доступа к моделям, платные генерации, прослушивание, Telegram, миграции и деплой
не проверялись. Offline tests устанавливают совместимость конфигурации и format contracts, а не factual accuracy LLM,
акустическое качество, availability или latency провайдера. Cap 1024 не гарантирует отсутствие MAX_TOKENS на любом входе.
