# Архитектура AI Response Delivery

**Дата:** 2026-08-14

**Статус:** одобрено для реализации

**Область:** provider streaming, Telegram streaming, Long Read, handlers

## 1. Контекст

Пользовательский дефект с пропавшей ссылкой Reader возник из-за разделённого
владения финальным Telegram-сообщением. Streaming implementation атомарно
добавляла Reader/Telegraph row, после чего handler вызывал
`edit_reply_markup()` и заменял всю клавиатуру своими action rows.

Расследование выявило связанные архитектурные причины:

- `stream_and_display()` совмещает provider orchestration, lifecycle,
  formatting, Telegram edits, Long Read, Redis, Telegraph, actions, metrics и
  logging;
- provider metadata возвращается скрытым каналом через `ContextVar`, причём
  provider modules импортируют setters из presentation module;
- provider race вручную переносит только часть metadata между asyncio tasks;
- ожидаемые provider failures кодируются как пользовательский текст с
  невидимым тегом;
- Long Read implementation продублирована в streaming и AgenticSearch и уже
  имеет разные fallback semantics;
- orchestration меняет private state `StreamingWriter`;
- ранние returns обходят cleanup delayed-feedback task и network-stall state;
- фоновые Telegraph tasks обходят общий `TaskManager`;
- `StreamingUIAdapter` имеет один production adapter и выдаёт raw Telegram
  `Message` наружу.

Локальное исправление сохраняет Reader row, но не устраняет сам класс ошибок.

## 2. Цели

1. Один module владеет финальным Telegram text и всей action keyboard.
2. Provider stream имеет типизированный interface и ровно один terminal event.
3. Finish reason, usage, grounding, actual route, failure и deferred state
   передаются явно, без `ContextVar` и error-as-text.
4. Streaming и уже готовые ответы используют одну Long Read implementation.
5. Cleanup выполняется для completion, partial failure, pre-text failure,
   deferred и cancellation.
6. Handlers получают именованный outcome и не знают ordering streaming
   implementation.
7. Старый interface и временные migration adapters удаляются до завершения
   работы.
8. Текущее пользовательское поведение сохраняется либо становится строго
   надёжнее без удаления возможностей.

## 3. Не-цели

- Унификация streaming outcome с существующим non-streaming `AIResponse`.
- Универсальный cross-platform UI interface: production UI сейчас только
  Telegram.
- Полная переработка key-health scoring и model selection вне требований нового
  event contract.
- Универсальная семантическая модель всех Telegram buttons.
- Изменение пользовательских callback data или порядка существующих base
  action rows.

## 4. Обязательная совместимость

Миграция считается завершённой только при сохранении следующих возможностей.

### Provider generation

- Gemini, OpenRouter, Opencode и FreeTheAI routing.
- Два competing keys и текущий Vertex slot там, где он разрешён.
- First non-empty text wins.
- Key rotation и model/cross-provider fallback до первого текста.
- Запрет смешивать ответ с другим key/model после первого текста.
- Key success/penalty accounting и фактически использованный provider/model.
- Native grounding и prompt-injected search context.
- Grounding sources победившего stream.
- Exact token usage, если provider её вернул; `None`, а не `0`, если неизвестно.
- Finish reasons STOP, MAX_TOKENS, SAFETY, RECITATION и provider-specific OTHER.
- Deferred generation только до появления текста.
- Cancellation losers без штрафа и с обязательным await.

### Telegram delivery

- Progressive edits с debounce и bounded retry на flood/rate-limit.
- Delayed high-load status и cancel action до первого текста.
- Остановка heartbeat на первом видимом тексте.
- Лимит 4000 применяется после Markdown → Telegram HTML и sanitization.
- Перенос незакрытых fenced code, inline code, bold, italic, underline и
  strikethrough между сообщениями.
- Action rows находятся только на последнем сообщении split chain.
- Reader/Telegraph row всегда первая; base action rows сохраняют порядок.
- Interrupted Long Read сохраняет publication row и использует recovery actions.
- Expandable summary остаётся около 800 символов.
- Redis Reader TTL остаётся 24 часа.
- Telegraph URL остаётся permanent fallback после истечения Reader content.
- `[VOICE]` никогда не показывается, даже если tag разделён между chunks.
- Intent/suggestion tags удаляются из финального текста.
- Voice, TTS, reactions, history persistence, memory footer и metrics остаются.

### Production consumers

- Chat streaming.
- Quick QnA/search streaming.
- Photo and media-group vision.
- Document Q&A.
- AgenticSearch completed response.
- Deferred worker.
- Inline provider consumers и grounding sources.
- Direct horoscope provider consumer.

## 5. Целевая архитектура

```text
Handler
  ├─ streamed response ──> TelegramResponseDelivery.stream(...)
  │                           ├─ AIStreamCoordinator
  │                           │    └─ ProviderRouter.stream(...)
  │                           │          └─ typed GenerationEvent
  │                           └─ TelegramRenderer session
  └─ completed response ─> TelegramResponseDelivery.deliver(...)
                              └─ тот же TelegramRenderer session

TelegramRenderer
  ├─ stateful TelegramRenderSession
  └─ one Long Read fallback chain
       ├─ Redis Reader
       ├─ Telegraph
       └─ Telegram split
```

Handler-facing module намеренно Telegram-specific. Provider event seam остаётся
отдельным, потому что inline, deferred и intent consumers используют generation
без обычной message delivery.

## 6. Provider stream module

### 6.1 Request values

`GenerationRequest` содержит:

- ordered `models`: preferred model и явно разрешённые caller fallbacks;
- typed `PromptTurn` sequence;
- `system_instruction`;
- `RequestScope(user_id, chat_id)`;
- `ThinkingLevel`;
- `GroundingMode` (`NONE`, `PROVIDED_CONTEXT`, `PROVIDER_SEARCH`,
  `PROVIDER_SEARCH_REQUIRED`);
- `Workload` (`INTERACTIVE`, `QUICK_SEARCH`, `DEFERRED_RETRY`, `INLINE`);
- `allow_deferred`.

`PromptTurn` содержит `Role` и typed `PromptPart`:

- `TextPart(text)`;
- `ImagePart(data, mime_type, needs_compression, cache_key, task_type)`.

Persisted chat history может оставаться JSON-compatible. Преобразование в
typed prompt происходит один раз перед provider seam; provider adapters больше
не принимают произвольные dict/objects.

### 6.2 Events

```python
GenerationEvent = TextDelta | StreamCompleted | StreamFailed | StreamDeferred
```

`TextDelta` содержит непустой visible model text.

`StreamCompleted` содержит:

- normalized `FinishReason(kind, raw)`;
- `TokenUsage(prompt, completion, total, cached)` с nullable fields;
- `GroundingReport`;
- `RouteUsed(provider, requested_model, actual_model)`.

`StreamFailed` содержит:

- существующий `ErrorCode`;
- phase `BEFORE_TEXT` или `AFTER_TEXT`;
- retry/key disposition;
- sanitized diagnostic для logs;
- actual route, если известен.

`StreamDeferred` содержит queue task id. Пользовательский текст не является
частью provider event; он строится presentation implementation по typed status.

### 6.3 Event invariants

1. Zero or more `TextDelta`, затем ровно один terminal event.
2. После terminal event ничего не выдаётся.
3. Completion без текста нормализуется в `EMPTY_RESPONSE` failure.
4. Race winner выбирается только первым непустым `TextDelta`.
5. Pre-text failure разрешает rotation/fallback согласно routing policy.
6. После первого текста route фиксирован; failure становится partial outcome.
7. Losers отменяются и awaited в `finally`.
8. Metadata losers отбрасывается.
9. Deferred разрешён только до первого текста и запрещён для
   `DEFERRED_RETRY`.
10. Expected remote failures являются typed values. `CancelledError` и
    programmer/protocol errors остаются exceptions.

### 6.4 Provider adapters

Gemini, OpenRouter, Opencode и FreeTheAI являются реальными adapters к одному
provider event seam. Каждый adapter нормализует native chunks, finish reason,
usage, grounding и remote errors. `ProviderRouter` единолично владеет race,
rotation, fallback, key penalties и deferred enqueueing.

После миграции удаляются `_GroundingMeta`, `_last_finish_reason`,
`_last_token_count`, setters и imports из providers в `app.streaming`.

## 7. Telegram Response Delivery module

### 7.1 External interface

```python
class TelegramResponseDelivery:
    async def stream(
        self,
        target: TelegramTarget,
        generation: GenerationRequest,
        *,
        presentation: TelegramPresentation,
    ) -> TelegramResponseOutcome: ...

    async def deliver(
        self,
        target: TelegramTarget,
        completed: CompletedResponse | GenerationFailure | DeferredGeneration,
        *,
        presentation: TelegramPresentation,
    ) -> TelegramResponseOutcome: ...
```

`TelegramTarget` поддерживает существующий placeholder и отправку нового
message для deferred delivery.

`TelegramResponseOutcome` является union:

- `CompleteDelivery`;
- `PartialDelivery`;
- `FailedDelivery`;
- `DeferredDelivery`.

Complete/partial outcomes содержат отдельно:

- `content_text`: canonical cleaned answer для history, memory и TTS;
- `displayed_text`: текст с footer/notices;
- completion metadata;
- `voice_requested`;
- immutable `DeliveryReceipt`.

`DeliveryReceipt` содержит `DeliveryKind`, message ids, final
`TelegramMessageRef` и publication URL. Raw mutable `Message` не возвращается.

### 7.2 Presentation seam

`TelegramPresentation.prepare(facts)` является pure interface. Production
adapters:

- `ChatPresentation` удаляет intent/suggestion tags и строит dynamic keyboard;
- `FixedPresentation` строит QnA, photo, document и research keyboard.

Prepared value содержит canonical content, normal/recovery/failure actions,
footer и Long Read title. Actions остаются Telegram-native
`InlineKeyboardMarkup`; cross-platform abstraction не создаётся.

Порядок chat actions сохраняется:

1. suggestions;
2. intent action;
3. retry;
4. roles/branch;
5. listen/new topic;
6. forwarded-message save;
7. copy code;
8. facts;
9. feedback.

### 7.3 Coordinator ordering

1. Открыть renderer session.
2. Mark network-waiting.
3. Начать provider stream.
4. Буферизовать начало, пока `[VOICE]` нельзя однозначно распознать или
   исключить; удалить tag до первого Telegram edit.
5. На первом видимом тексте остановить heartbeat/delayed feedback и mark alive.
6. Передавать visible deltas renderer.
7. Получить ровно один terminal event.
8. Построить canonical `content_text`, удалив hidden response tags.
9. Для normal completion построить `displayed_text` как content + footer +
   normalized finish notice.
10. Для transport interruption построить content + interruption notice без
    normal footer.
11. Построить actions из typed final facts.
12. Вызвать renderer finalize ровно один раз.
13. В `finally` закрыть provider iterator, отменить/await request tasks и
    очистить network state.

`CancelledError` всегда повторно выбрасывается после cleanup. Expected
generation failures возвращаются typed outcome. Если Telegram после bounded
recovery не может ни edit, ни send, выбрасывается `TelegramDeliveryError`.

## 8. Telegram renderer session

Renderer session имеет внутреннее состояние:

```text
OPEN -> STREAMING -> FINALIZED -> CLOSED
OPEN -------------> FINALIZED -> CLOSED
```

Второй finalize или append после finalize является protocol error.

`TelegramRenderSession` владеет:

- raw/current/full buffers;
- formatting и sanitization;
- debounce;
- edit retry;
- split-point selection;
- Markdown continuity;
- current Telegram target;
- explicit delivery state.

Старый `StreamingWriter` не сохраняется даже как внутренний compatibility
layer: его ответственность перенесена в renderer session и Long Read fallback.
Coordinator не читает и не меняет private state. Final content replacement и
notice append выполняются session interface, сохраняющим invariant: terminal
prepared value является authoritative, даже если progressive draft отличался.

Публичный `StreamingUIAdapter` удаляется. Telegram transport остаётся private
test seam renderer с production и recording adapters.

## 9. Long Read delivery

Long Read выбирается по длине sanitized formatted HTML, а не raw Markdown.

Fallback chain фиксирована:

1. Если `WEBAPP_BASE_URL` настроен, сохранить displayed Markdown в Redis.
2. Только после подтверждённой записи сформировать Reader URL.
3. Показать expandable summary и Reader row через `replace_or_send`: сначала
   edit текущего message, при recoverable edit failure — send нового.
4. Только после успешного отображения вернуть `DeliveryKind.READER` и запустить
   background Telegraph cold storage.
5. Если Reader storage или presentation не удались, синхронно создать
   Telegraph page и показать Telegraph row через тот же `replace_or_send`.
6. Если Telegraph creation/presentation не удались, отправить полный ответ как
   Telegram split chain.
7. Если split также невозможно показать, выбросить `TelegramDeliveryError`.

Publication row добавляется в начало копии base rows; original markup не
мутируется. При split base actions прикрепляются только к последнему message.

Background Telegraph task регистрируется в общем `TaskManager`, чтобы shutdown
мог выполнить drain. Page creation выполняется один раз; если page создана, а
Redis URL write не удался, повторяется только storage operation, чтобы не
создавать duplicate Telegraph pages.

Delayed feedback, heartbeat и provider race tasks не направляются в глобальный
`TaskManager`: они принадлежат request lifecycle и структурированно закрываются
coordinator/renderer.

## 10. Error model

- Provider adapter преобразует expected SDK/HTTP errors в typed failure.
- ProviderRouter принимает решение о rotation/fallback и отдаёт failure наружу
  только после исчерпания допустимых вариантов либо после partial text.
- Presentation преобразует `ErrorCode` и typed status в локализованный текст и
  recovery actions.
- Generation failure не может попасть в `content_text`.
- Model MAX_TOKENS/SAFETY/RECITATION возвращают `PartialDelivery` с существующим
  notice; это не transport failure.
- Timeout/network/provider failure после текста возвращает `PartialDelivery` с
  recovery actions.
- Pre-text failure возвращает `FailedDelivery`; coordinator уже отображает
  ошибку, handler выполняет только domain rollback.
- Deferred является отдельным outcome, а не failure text.
- Cancellation не отображается как generic error и всегда re-raised.
- Diagnostics никогда не показываются пользователю.

## 11. Testing strategy

Interface является test surface. Старые tests private state удаляются после
появления эквивалентных interface-level tests.

### Provider contract tests

- adapter event sequence и единственный terminal event;
- exact finish/usage/grounding normalization;
- error values не являются text;
- empty completion → `EMPTY_RESPONSE`;
- first-text-wins race;
- metadata не выбирает winner;
- loser cancellation/await без penalty;
- pre-text rotation/fallback;
- отсутствие route switch после текста;
- winner metadata и actual route;
- deferred legality;
- cancellation propagation.

### Coordinator tests

- `[VOICE]` целиком и между chunks;
- intent/suggestion cleanup;
- complete, model-partial, transport-partial, failed и deferred outcomes;
- content/displayed separation;
- footer/notice ordering;
- no footer on transport interruption;
- provider iterator closed;
- delayed task cancelled/awaited;
- network state always cleared;
- cancellation re-raised после cleanup;
- renderer finalized exactly once.

### Renderer tests

- short message;
- post-HTML 4000 limit;
- split and Markdown continuity;
- actions only on final split message;
- Redis success → Reader;
- Redis failure → Telegraph;
- Reader edit failure → send-new recovery либо Telegraph;
- Telegraph failure → split;
- Telegram total failure → `TelegramDeliveryError`;
- publication row first, base rows unchanged;
- interrupted Long Read + recovery actions;
- summary formatting/sanitization;
- state-machine misuse;
- background Telegraph registered and shutdown-drainable.

### Handler and end-to-end tests

- chat history/TTS/reactions из typed outcome;
- QnA routing/grounding без error-tag scan;
- photo/document без tuple unpacking и duplicate send;
- AgenticSearch использует тот же Long Read module;
- deferred worker использует completed delivery;
- inline grounding использует typed events;
- исходный Reader keyboard regression;
- no-link failure paths;
- unchanged callback data and action order.

Каждая production change выполняется TDD: новый test сначала должен упасть по
ожидаемой причине, затем minimal implementation делает его зелёным.

## 12. План миграции

### Phase 0 — Behaviour lock

Добавить недостающие characterization/regression tests для текущих сценариев и
трёх обнаруженных failure paths. Зафиксировать callback data, action order,
formatting, Long Read chain и lifecycle cleanup.

### Phase 1 — Typed provider values

Добавить request/event/outcome values и contract validation. Мигрировать
Gemini, OpenRouter, Opencode и FreeTheAI adapters, сохранив current routing
policy. Временный conversion code разрешён только внутри migration branch.

### Phase 2 — ProviderRouter events

Перевести single-key, race, fallback и deferred ветки на typed events. Затем
мигрировать direct consumers: inline, deferred worker и intent router. Удалить
`ContextVar`, `_GroundingMeta` и streaming error-as-text.

### Phase 3 — Telegram renderer and Long Read

Создать Telegram renderer/session, internal writer и единый Long Read delivery.
Сначала провести completed AgenticSearch через `deliver()` и удалить его
дублированную implementation.

### Phase 4 — Coordinator

Создать coordinator поверх typed provider events и renderer. Реализовать voice
prefix buffering, final preparation, typed outcomes, metrics и гарантированный
lifecycle.

### Phase 5 — Handler migration

Перевести chat, QnA, photo и document на `stream()`. History не мутируется до
успешного/partial outcome, поэтому rollback callbacks удаляются. TTS и reactions
используют typed outcome и immutable final message reference.

### Phase 6 — Deferred and remaining paths

Deferred worker доставляет completed response через `deliver()`. Проверить все
production searches на старые string chunks, tuple unpacking и post-final edits.

### Phase 7 — Deletion and documentation

Удалить `stream_and_display`, старый public `StreamingWriter` use,
`StreamingUIAdapter`, local task registries, duplicate Long Read, obsolete tests
и migration adapters. Обновить `CONTEXT.md`, `docs/ARCHITECTURE.md` и записать
ADR о единственном владельце Telegram response delivery.

## 13. Completion criteria

- В production нет `stream_and_display` и шестипозиционных stream tuples.
- В providers нет imports из presentation/Telegram modules.
- В streaming paths нет `ContextVar`, `_GroundingMeta` и error-as-text.
- Ни один handler не редактирует final response keyboard после delivery.
- Long Read implementation существует в одном месте.
- Все background Telegraph tasks видимы `TaskManager`.
- Все request-scoped tasks закрываются на каждом exit path.
- Все compatibility scenarios из раздела 4 покрыты tests.
- Relevant unit/contract/e2e suites, Ruff и Mypy проходят.
- Старый migration code удалён в той же ветке.

## 14. Риски и контроль

Главный риск — широкий provider contract затрагивает direct consumers за
пределами Telegram streaming. Поэтому миграция выполняется отдельными зелёными
phases, но target architecture не содержит постоянного compatibility façade.

Второй риск — текущий worktree содержит независимые изменения model catalog в
provider/router files. Реализация обязана сохранять их, применять узкие patches
и коммитить только файлы текущей архитектурной работы.

Третий риск — расхождение streaming draft и canonical final text. Renderer
обязан считать terminal prepared value authoritative и выполнить final replace
до выбора Long Read publication.
