# Аудит промптов — 2026-10-02

Проверены все **83 исходные записи** реестра в рабочем дереве и связанные с ними
вызовы: входные переменные, формирование запросов, парсеры, схемы, ограничения
доставки и fallback. При трассировке найдены ещё **7 встроенных инструкций**;
основной агент подключил их к каталогу. После изменений каталог содержит
**90 записей: 83 действующие и 7 исторических без активного потребителя**.

Это аудит исходного кода и шаблонов по умолчанию, а не выгрузка промптов работающего
VPS. Сохранённые административные overrides, персональные роли и приватные данные
из БД не читались. Override продолжает иметь приоритет над обновлённым baseline.
Уже подготовленные карточки, картинки и персональные копии ролей автоматически
не пересоздаются от изменения исходного текста.

Исправлены подтверждённые несоответствия контрактам и неоднозначные инструкции.
Восемь новых offline-проверок воспроизвели семь ошибок до исправлений и прошли
после них. Направленные улучшения формулировок отмечены отдельно: без вызовов
моделей нельзя утверждать измеренный прирост качества, точности или устойчивости
к prompt injection.

## Критерии

Сначала проверялось соответствие фактическому исполнению, затем ясность задачи,
отделение исходного материала от инструкций, совместимость примеров с форматом
ответа, поведение при неполных данных и отсутствие обещаний несуществующих
инструментов. Схемы, enum, имена placeholders и специализированные provider paths
сохранены. Модели, ключи, квоты, consent/lease и правила удаления этой частью
аудита не менялись.

Принципы прямых инструкций, согласованных примеров и явной структуры сверены с
[официальным руководством Gemini по промптам](https://ai.google.dev/gemini-api/docs/prompting-strategies).
Контракт JSON дополнительно сопоставлен с
[документацией structured outputs](https://ai.google.dev/gemini-api/docs/structured-output):
формат примера не заменяет проверку фактического ответа приложением.
Для TTS учитывалось различие чтения заданного текста и разговорного Live API,
описанное в [руководстве TTS](https://ai.google.dev/gemini-api/docs/speech-generation).
Эти ссылки не служат основанием для миграции SDK или смены моделей.

## Найденные проблемы и изменения

| Область | До исправления | Изменение и доказательство |
| --- | --- | --- |
| Одиночное фото / альбом | `vision.describe` требовал анализ группы, `vision.describe_group` — одного изображения; выбор ID в builder был правильным. | Исправлены тексты под фактическую кратность входа. Два новых теста вызывают `_build_vision_prompt` с 1 и 3 изображениями; оба падали до правки. Ответ на конкретный вопрос теперь приоритетнее шаблонного описания. |
| Примеры JSON | Role schema содержала `... (3-7 элементов)`; duplicate judge — `true\|false` и `0.0..1.0`; memory relevance — голое `...` внутри массива. | Примеры стали валидным JSON, ограничения вынесены в прозу. Новые тесты декодируют примеры и передают их role consumer / Pydantic schema. Это проверка совместимости примеров, не точности ответа модели. |
| Trivia | `correct_index=0` ссылался на «Вариант A», а `key.answer` содержал другой ответ. Не были определены нулевая индексация и единственность правильного варианта. | Оба примера согласованы с `options`, `correct_index` и `key.answer`; объём 5/3 вопросов сохранён. Тесты прогоняют пример через shuffle/parser и validator и проверяют, что answer соответствует выбранному варианту. |
| Refine summary | Инструкция «не повторяй то, что уже есть» поощряла выдавать только добавление, хотя код целиком заменяет старое резюме. Требование сохранить весь код дословно противоречило лимиту. | `summary.refine` требует полное обновлённое резюме. Заданы приоритеты в лимите, атрибуция, различие планов и выполненных действий. Отдельный найденный дефект последовательной подстановки placeholders исправлен основным агентом в summarizer с regression test. |
| Финальный research synthesis | При выходе из цикла новая system instruction теряла правила источников, языка и неопределённости. | Synthesis теперь сохраняет grounding, ссылки только из tool results, язык вопроса и честное описание пробелов. Page budget не выдаётся за число прочитанных страниц. Агент учитывает реальные отказы/лимиты инструмента, различает личную память и публичные доказательства. |
| Служебные теги чата | И INTENT, и SUGGESTIONS требовали оказаться последними. Инструкция предлагала «выполнять напрямую» даже без доступного действия. | Один порядок: основной текст → INTENT → SUGGESTIONS; `[VOICE]` остаётся в начале. Не добавляются новые теги/enum. Подсказки исключают символы-разделители парсера. Выполнение действий не объявляется без результата. |
| TTS | Директор требовал читать всё по-русски независимо от текста; пример произношения русского «ИИ» был «ай-ай». | Сохранение языка каждого фрагмента, русское «и-и», числа читаются естественно. Произносимый текст отделён от указаний темпа; команды внутри него не исполняются. Прослушивания синтезированного аудио не было. |
| Память | Численный минимум мог требовать выдумать факты; текущая работа, дом и состояние здоровья автоматически описывались как permanent. `NEW` путалось с новым фактическим состоянием. | Разрешены меньше фактов и пустой результат. `is_core` требует явной устойчивости и не отменяет consent/deletion; описание поля schema согласовано с промптом. Сохранены negation, время и атрибуция; при недостатке доказательств конфликт не должен закрывать старое ребро. Источники и индексы берутся только из входа. |
| Поиск по памяти | Запрос без извлечённых воспоминаний мог дополняться вымышленной биографией. | Expansion сохраняет язык, сущности и неопределённость, не угадывает отсутствующие имена/технологии. Relevance выдаёт JSON booleans и только переданные индексы. |
| OCR, ASR, изображение | Vision classifier отправлял перевод текста в OCR, который запрещал всё кроме дословного извлечения. ASR требовал только transcript и одновременно metadata; стабильные фактические вопросы не попадали ни в одну ясную категорию. Image extraction/translation мог терять стиль, отрицания и переводить нужную надпись. | OCR допускает явно запрошенный перевод, иначе сохраняет оригинал, помечает неразборчивое. ASR явно задаёт transcript + существующие metadata и категории. Для изображения сохраняются композиция, стиль, исключения и точная надпись; отсутствующий референт не выдумывается. |
| Crocodile | Отдельная общая подсказка одновременно должна была однозначно отличать ответ; базовый plain-text prompt спорил с добавляемым JSON-форматом. «Можно потрогать» противоречило разрешённым видимым явлениям. | Уникальность относится к трём подсказкам вместе, первая остаётся общей. Category/fast word явно допускают формат, который задаёт вызывающая ветка. Банк выдаёт JSON-массив, drawable-description соблюдает уже существовавшие 2–150 символов. Команды игрока не задают судейскую оценку. |
| Документы, доски, briefs, роли, natal | Не везде исходный текст был явно отделён от указаний. Role generation приписывала стаж и выполненные проверки. Natal repair принуждал к русскому после параметра языка и мог потерять ограничения неизвестного времени. | Добавлены границы данных и атрибуция, запрет выдуманных ссылок/дедлайнов/согласия. Роли сохраняют задачу и реальные доступы, редактор сохраняет нужные placeholders. Natal repair подчиняется языку и `houses_available`; сырые данные рождения не добавляются. |
| Неподключённые карточки | Три прежних search-шаблона и четыре прежних summary-шаблона показывались как будто управляют текущим исполнением. | Основной агент пометил их `inactive`, запретил новые save/reset и указал действующие ID. Исторические overrides сохраняются для совместимости. |

## Полный охват каталога

В таблице перечислены все 90 ID. Запись `prefix.{a,b}` обозначает перечисление
`prefix.a`, `prefix.b`, а не wildcard. Количество включает исторические записи.

| Группа | ID и количество | Потребитель / проверенный контракт |
| --- | --- | --- |
| Общение и стандартные роли | `system_prompt_full`, `system_prompt_compact`, `formatting_rules`, `formatting_rules_compact`, `voice_tag_instruction`, `intent_routing_instruction`, `smart_suggestions_instruction`, `role.{teacher,it_engineer,doctor_info,gardener,lawyer_info,productivity_coach}`, `chat.continue` — **14** | `PromptRegistry.compose_system_prompt`, chat delivery, `response_tags`, continue callback. Пресеты ролей копируются при выборе; сохранённые персональные копии не переписываются. |
| Создание и редактирование ролей | `prompt_engineer`, `roles.edit` — **2** | `msg_roles`, `cb_roles`, `extract_json_object`; поля title/purpose/system_prompt и нормализация в prompt, либо полный plain-text результат редактора. |
| Поиск / research | `search.native`, `research_agent_system`, `research.synthesis.{system,request}` — **4** | Native grounding, AgenticSearch tool declarations, `conclude_research` и вынужденный synthesis; Markdown и фактически полученные URL. |
| Исторические шаблоны | `qna_localization`, `url_selection`, `synthesis`, `summarization_system`, `summarization_chunk`, `summarization_refine_first`, `summarization_refine_subsequent` — **7** | Нет активных domain callers. Показаны как архивные; реальные replacements перечислены в `prompt_usage.py`. |
| Сжатие контекста | `summary.{system,chunk,first,refine}` — **4** | `context/summarizer.py`, замена целого summary на каждой итерации, бюджет и literal braces. |
| Inline | `inline.system`, `inline.tabs`, `inline.search.{enabled,disabled}` — **4** | `_generate_and_edit_inline`; опциональная XML-оболочка с Markdown внутри и фактический флаг поиска. |
| Фото и документы | `image_analysis`, `vision.{intent,search_group,describe,describe_group,ocr}`, `document.answer`, `media.image_description`, `media.document_summary` — **9** | `ai_photo`, `ai_document`, `vision_intent`, multimodal processor; cardinality, OCR/describe enum, caption, Markdown и отсутствие выдуманного содержимого. |
| ASR и voice intent | `media.asr`, `media.voice_memory`, `media.intent.text`, `intent.voice`, `intent.voice.system` — **5** | Transcript + `INTENT:CONVERSATIONAL/TRANSCRIPTION/SEARCH/DRAW`, опциональный `DRAW_PROMPT`, memory tone tags, YES/NO classifier. |
| TTS и Live | `tts.{director,style.neutral,style.expressive,style.conversational}`, `live.default`, `live.vertex.search` — **6** | Gemini TTS director + pacing tag + cleaned text; Live language rules и поиск только в соответствующей Vertex-сессии. |
| Подготовка image prompt | `image.prompt_extract`, `image.translate` — **2** | `cmd_image.check_draw_intent_async`, перевод генератору; descriptive string / NONE, точный текст внутри изображения. |
| Память | `memory.{extract,taxonomy,consolidate,consolidate.fallback,expand,relevance}` — **6** | Pydantic graph, update/parallel/refinement, source_ids/support_fact_indexes, plain bullets fallback, search phrase и indexed booleans. |
| Trivia | `trivia.{main,super,deduplicate,audit,explain}` — **5** | Вопросы 5/3, четыре уникальных options, индекс 0..3, FactIdentity, semantic audit schema. Фактологическую верность всей генерации offline-тесты не устанавливают. |
| Crocodile | `crocodile.{word,hints,judge.classic,hints.classic,hints.batch,category,words.bank,words.fast,image_prompt,image.scene}` — **10** | Word/hints JSON, GuessJudgement, category/fast selected-model branches, bank array и scene для image provider; специализированный прямой Gemini path сохранён. |
| Natal и гороскоп | `natal.{interpretation,repair}`, `horoscope.system` — **3** | ChartData, stable section IDs, parser/repair, отсутствие сырых birth inputs и ограничение неизвестного времени; астрологическая интерпретация не объявляется установленным фактом. |
| Tarot | `tarot.daily`, `tarot.inline.{classic,daily,yes_no,love,celtic,fallback}` — **7** | Реально выбранные карты и ориентация, 1/3/5/6-card инструкции, Markdown, 3500-character guidance для Celtic. Форматы и игровые трактовки сохранены. |
| Доска / brief | `board.synthesis`, `brief.summary` — **2** | Plain text c авторами и лимитом 800 символов; JSON headline → summary, исходные article URLs. |

Исходные 83 ID покрыты полностью. Дополнительные семь находятся в
[additional_prompts.py](../app/runtime_settings/additional_prompts.py):
`horoscope.system`, `crocodile.image.scene`, `inline.tabs`,
`inline.search.enabled`, `inline.search.disabled`, `chat.continue`,
`media.intent.text`. Их wiring и тесты принадлежат основной части изменений.

Не всякая строка вокруг provider call является ещё одним редактируемым пресетом.
Tool declarations, Pydantic descriptions, runtime limits/errors, вычисленные
ограничения качества ChartData, сообщения пользователя и данные карт — части
контракта или входные данные. Внешние image/audio providers, принимающие готовый
текст, также не получают автоматически chat-system prompt. Их поведение нельзя
приравнивать к управлению `system_prompt_full`.

## Проверки

- [Новые contract tests](../tests/test_prompt_quality_contracts.py): **8 passed**.
  Первый запуск до исправлений: **7 failed, 1 passed**. Проверяется рендер всех
  зарегистрированных baseline с вложенным JSON/буквальными braces, кратность
  vision-входа, декодирование JSON-примеров, role consumer, Trivia parser и
  соответствие answer/index. Эти тесты не симулируют качество модели.
- Итоговый целевой прогон **255 passed** за 18,80 с: prompt quality/registry/
  composition/controls, OCR, response tags, Daily Trivia, natal, voice intent,
  multimodal, memory query/extraction/consolidation, briefs, agentic search и
  Daily Crocodile models. Использовано `uv run --locked pytest` с
  `--override-ini="addopts=" --timeout=30`; только offline fixtures.
- `uv run --locked ruff check` для 21 изменённого файла этой части: **passed**.
- `uv run --locked ruff format --check` для тех же 21 файлов: **passed**.
- `python -X utf8 scripts/check_encoding.py`: **passed**, 116 Markdown-файлов
  после записи отчёта; до правок baseline также прошёл проверку.
- Относительные ссылки отчёта проверены `check_document` из
  `scripts/check_docs_links.py`: **passed**. `git diff --check`: **passed**.

Результаты полной offline-suite и общих gates всего рабочего дерева приведены
в [runtime controls](runtime-controls.md#проверка-и-ограничения); этот отчёт
не подменяет их своим целевым прогоном. Сеть к Telegram,
LLM/TTS/image providers, рабочая БД, миграции и deployment не использовались.

## Остаточные ограничения

1. **Исполнение промптов вероятностное.** JSON examples, output limits и запрет
   следовать вложенным командам помогают сформулировать контракт, но не доказывают
   его соблюдение моделью. Фактологическая точность Trivia, восстановление речи,
   произношение TTS, полнота summary и устойчивость к injection требуют отдельной
   репрезентативной выборки и реальных provider evaluations.
2. **Промпт не заменяет enforcement.** Board 800 символов и отдельные языковые
   правила не являются жёсткими гарантиями. Проверка research citations в коде
   диагностическая; output boolean/index validators различаются по процессам.
   В данном проходе парсеры, truncation и схемы не перепроектировались.
3. **Покрытие реестра не означает, что все поля независимы.** Shared formatting,
   роль и локальная задача композиционно взаимодействуют; администратор всё ещё
   может написать логически противоречивый override с корректными placeholders.
   Валидация формы не является semantic lint.
4. **Приватность обеспечивается кодом.** Consent epochs, leases, scope,
   source provenance и deletion остаются механизмами защиты. Новые фразы в
   memory/natal prompts не заявляют дополнительной изоляции и не гарантируют
   удаление ранее опубликованных сторонних копий.
