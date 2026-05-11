  - **Tabbed Response UI**: Inline responses are structured using XML tags (`<tldr>`, `<details>`, `<sources>`) extracted by the LLM and rendered dynamically via inline buttons. Users can seamlessly switch tabs without re-triggering generation. Admin-toggleable via `/set_inline_tabs <on|off>`.
  - **Collaborative AI-Notes**: Prefixing a query with `доска: <topic>` initializes a persistent, shared workspace. Any user can reply to the board to add notes (bypassing privacy mode via `via_bot` detection). The bot debounces new entries (60s window) and automatically synthesizes them into an evolving structural summary via the `TaskManager`. Backed by PostgreSQL `inline_boards`.
  - **Core Architecture**: Powered primarily by **`gemini-3.1-flash-lite` via Vertex AI Express** with native **Google Search Grounding** (`enable_web_search=True`) as the **primary inline slot**. AI Studio keys (`gemini-2.5-flash-lite`) race alongside as fallback slots via `_stream_inline_fast()` (3-way Race Requests, up to 12 slots across 4 rounds). The first valid response wins; Vertex AI Express wins most races due to lower latency and higher quota stability. Infrastructure failures raise `ProviderOverloadError` and surface a "⏳ Серверы ИИ перегружены" prompt instead of a generic error. Grounding citations are surfaced as an expandable `📎 Источники` blockquote (up to 3 URLs) at the end of the response. **5-Mode Smart Image Routing**: inline queries automatically detect intent — quoted text (`«»""`) → `wan-image` (Мем/Текст), edit verbs → `klein` (Изменить фото), or manually selectable: `zimage` (Турбо), `gptimage` (Умный), `qwen-image` (Арт). Background generation tasks are managed by the centralized `TaskManager` (graceful shutdown drain, MAX_TASKS=100 cap). On failure, translation loops gracefully trap errors and attach a **🔄 Повторить** inline retry button for one-tap re-generation. Requires `/setinline` + `/setinlinefeedback` at 100% in BotFather.
- **Agentic Web Browsing (`??` prefix)**: Deep research mode utilizing Tavily API and Jina Reader API for multi-step query decomposition, autonomous site triage, content extraction, and dynamic self-correction loops. Hardened against memory leaks caused by gRPC protobuf cyclic references during long-running iterations (including threaded, non-blocking asynchronous Garbage Collection). Per-call API key usage tracking ensures accurate quota accounting across all LLM invocations within the agentic loop. Features an intelligent **Model Fallback Cascade** (automatically retries failed LLM requests or 503 errors using the next most capable model according to the capability tier rankings), parallel tool execution (`asyncio.gather` with semaphore), two-layer page content caching (session + global, 30-min TTL), source quality scoring (domain classification, freshness labels, citation validation), adaptive iteration budget (query deduplication, configurable token cap and wall-clock timeout), and rich streaming progress with search queries and iteration counters.
- **Image Processing Pipeline**: Context-aware adaptive resize (`TASK_DIMS`: describe 1280px, search 768px, OCR 2048px) governed by **Shannon Entropy Analysis** (dynamically boosts +50% dimension for text-dense screenshots while reducing -25% for simple photos, optimizing token usage). Uses a 3-stage compression pipeline (thumbnail → JPEG q85 → fallback q75/65), TTL-cached results (`cache_key` by `file_unique_id`), and `TaggedImage` metadata carrier across handler→provider boundary to eliminate redundant recompression. Media group downloads use `Semaphore(5)` with debounced progress indicator.
- **Image Generation (Multi-Provider)**: Text-to-image generation via `/draw <prompt>` or via **implicit natural language triggers** (e.g., *"Бот, нарисуй кота"* / *"сгенерируй картинку леса"*). Uses a multi-layered Regex heuristics engine to isolate the artistic prompt without leaking pronouns or conversational fillers (e.g. extracts "леса" from "сгенерируй мне пожалуйста картинку леса"). Implicit triggers are natively intercepted in both text and voice channels. Voice requests trigger an **Interactive Pre-Canvas Confirmation** where the parsed text and generation keyboard are rendered interactively before consuming API resources. Uses a Factory Pattern for provider routing:
  - **Google Imagen 4** (`imagen-4.0-fast-generate-001`, etc.): Triggered when the user requests an `imagen-*` model and `GEMINI_API_KEYS` are available. Features an **isolated per-key RPD budget** to protect chat quota.
  - **Pollinations.ai** (Models: `✨ Flux`, `⚡ Z-Image`, etc.): The primary provider for free-tier keys, capable of operating completely without an API key. Uses robust transport layer fallback: attempts OpenAI-compatible `POST` for structured errors, failing over to a direct keyless `GET` stream with `Content-Type: image/*` validation if the primary endpoint throws budget exhaustion (402/401) or 5xx timeouts. Includes user-friendly messaging for 429 rate limits.
  - **FreeTheAI Router** (Models: hr/gpt_image_2, hr/nano_banana_2, etc.): Triggered for hr/* prefix models, acting as a gateway to advanced external image models.
  - **Interactive Canvas UX**: Features full, unrestricted prompt display (up to 800 characters) across the entire UI. Heartbeat animation (`ChatAction.UPLOAD_PHOTO` refreshed every 4.5 s) during generation, followed by an inline keyboard for one-tap regeneration, aspect ratio switching (1:1, 3:4, 4:3, 9:16, 16:9), and dynamic model switching. Includes a native "✏️  Изменить промпт" pasteboard workflow for frictionless prompt editing. The model selection buttons are auto-generated from environment variables (`IMAGE_MODELS`) with smart column-balancing.
- **Document Understanding**: Extracts text from PDF/DOCX files and uses it for context-aware Q&A.
- **Multimodal Processing Pipeline**: Voice messages transcribed via `gemini-3.1-flash-lite` (high thinking budget for ASR quality) with intent-aware routing (`INTENT:CONVERSATIONAL`, `INTENT:TRANSCRIPTION`, `INTENT:SEARCH`). Features **Smart Voice Auto-Routing** (bypasses manual confirmation UI for low-complexity transcripts) with regex-based fluff tolerance, **Voice-to-Search Auto-Routing** (when `INTENT:SEARCH` is detected, directly invokes **WeatherAPI.com** for weather (1 request, localized RU conditions + "feels like"), **ExchangeRate-API** for fiat currency (RUB/KZT/UAH support), and **CoinGecko** for crypto (BTC/ETH/SOL/TON with Russian aliases) — zero LLM cost — then falls back to **QnA Grounded Search** via `gemini-2.5-flash-lite` with native Google Search Grounding for general factual queries; for users in deep-dive or search-enabled mode, routes to the full **Agentic Research Pipeline** instead), **QnA History Persistence** (QnA voice search results are saved to `chat_state.history` and persisted via `update_user_chat`, matching the deep research path — previously these turns were silently dropped), and **Show & Tell** (voice-replies to photos dynamically inject the image into the LLM context). The **Voice Engine 5.0** pipeline powers outbound voice replies using **ElevenLabs TTS** as the primary provider with atomic fallback to Gemini REST TTS (`gemini-2.5-flash-preview-tts`). Audio is transcoded into Telegram-compliant PCM→OGG Opus via `ffmpeg` at **24k bitrate** (optimized for speech). The Gemini TTS engine uses **Parallel Batch Chunking** (800 max bytes, up to 2 chunks concurrently via `asyncio.gather`) to prevent API timeouts while conserving the 15 RPD (Requests Per Day) budget per key, and features a **Future-Based Pre-Generation** architecture: TTS generation starts immediately at enqueue time across all queued messages, while per-user delivery order is preserved by a FIFO worker that simply awaits pre-computed audio futures. If the pre-generation future fails (including `asyncio.CancelledError`, which is a `BaseException` in Python 3.14), the worker gracefully falls back to synchronous retry. Combined with **Asynchronous Race Requests** (2 keys per chunk, first-to-finish wins), this eliminates serialisation bottlenecks when multiple voice replies are pending. Concurrency is capped at 3 simultaneous TTS jobs (`GEMINI_TTS_CONCURRENCY=3`) to ensure worst-case burst (3 jobs × 2 chunks × 2 key-racing = 12 RPD) consumes at most 1 key from the 10–12 key pool. A highly optimized Steerable Voice prompt enforces **Strict Verbatim Constraints** and features **Dynamic Personalities** tied directly to the MiniApp's **Independent TTS Temperature** slider (shifting between strict news-anchor, conversational, or highly engaging storytelling). Finished with a low-threshold PCM silence trimming gate (400 amplitude). Featuring **Zero-Latency Voice Intent Detection** (`[VOICE]` tag stripping). Users can hit the **Re-transcribe (Flash)** button for stubborn transcriptions. Media types stored as long-term memories via `submit_retryable()` tasks.
- **Resilient Streaming & Mid-Stream Error Recovery**: Gracefully handles API failures (like 503 Service Unavailable) that occur *during* active streaming. The payload stops, the chunking layer intercepts the error, and a localized footer (`⚠️  ответ был прерван из-за ошибки сервера`) is appended. Users are immediately shown `[▶️  Продолжить]` and `[🔄 Заново]` recovery buttons to seamlessly inject partial output back into the conversation context, allowing the LLM to resume generation without context loss. Features a stabilized streaming state machine that eliminates legacy UI placeholder overrides (Phantom Draft Mode cleanup). **Delayed UX Feedback**: if no chunks arrive within 5 seconds, a transparent status toast (`⏳ Запрос в обработке...`) with a `[⚙️ Отменить]` inline button replaces silent waiting. **TTFB-based Stall Tracking**: detects genuinely stalled connections (>15s waiting for HTTP headers) and allows new user messages to cancel only stale tasks while preserving healthy active streams. **Gemini Search Hallucination Filter** (v2.12.11): `StreamingWriter.write()` now strips only explicit leaked internal `[tool_code] ... google_search.search(...)` traces while preserving legitimate fenced code samples and explanatory references.
- **Internationalization (i18n)**: Content-based language detection (Cyrillic density heuristic) with full bilingual UI (Russian/English). All user-facing strings externalized to `app/i18n.py` registry with `t(key, lang, **kwargs)` lookup. Language detected from message content, not Telegram settings.
- **Python 3.14 Deprecation Cleanup**: Runtime callback awaitability checks now use `inspect.iscoroutinefunction()` in the remaining helper paths instead of deprecated `asyncio.iscoroutinefunction()`, reducing deprecation noise and keeping the code aligned with Python 3.14+ guidance.
- **Persistent GraphRAG Memory**: Semantic recall via `pgvector` (`halfvec(768)`) with **Adaptive Thresholding RRF** retrieval (cosine similarity + `pg_trgm`, over-fetch ×2 then gap-filter ≤15pp from top score). **LLM-as-Judge Fallback**: when primary search (floor ~0.48) returns nothing, a second pass at floor 0.42 feeds low-confidence candidates to Flash-Lite, which judges each for genuine relevance — a "recollection path" inspired by RF-Mem (2025) dual-process memory. **2-Hop Knowledge Graph Traversal** (SQL CTE `hop1 ∪ hop2`) surfaces indirect relationships, e.g. FastAPI when asking about Python. **Multi-Query Expansion** rewrites vague queries (Flash-Lite LLM, ~200ms) into keyword-dense search phrases before embedding. **Semantic Edge Deduplication** (cosine < 0.25) merges near-identical predicates at consolidation time. **Core Persona Protection**: edges marked `is_core=TRUE` (name, profession, allergies) bypass time-decay and always rank first in graph context. Features **Semantic Entity Resolution** (`< 0.12` distance merging) to prevent graph fragmentation, and **Temporal Edge Upserts** (`ON CONFLICT` updates). Voice and media memories are **Enriched with Modality/Tone Tags** (e.g., `[VOICE, Tone: X]`) via dedicated system prompts. System clusters relational knowledge into dual tables (`memory_nodes`, `memory_edges`) for entity graphing. Memories injected into `system_instruction` as structured `<memory_palace>` XML tags (Context Engineering). Only user intent is embedded for maximum vector density (`source_type='user_intent'`). Dynamic consolidation triggers at ~8,000 tokens or 7 days, extracting atomic persona facts and relationships via LLM. User-manageable via `/memory` (paginated inline UI with per-item delete) and toggleable via `/settings`.
  - **MemPalace Wing/Room Taxonomy**: Every memory and graph entity is classified into a 5-wing hierarchy (`identity`, `projects`, `social`, `knowledge`, `temporal`) with 4—5 rooms each and 6 hall types (`fact`, `opinion`, `event`, `plan`, `preference`, `habit`). Classification via LLM (admin-configurable model via `TAXONOMY_MODEL`). Partial HNSW indexes on high-traffic wings for sub-10ms targeted retrieval.
  - **AAAK Tiered Context Compression**: 4-layer memory stack inspired by AAAK lossless shorthand — L0 core facts (JSON shorthand, ~250 tokens), L1 active context (consolidated memories + role diary, ~600 tokens), L2 semantic recall (graph-augmented search, ~1500 tokens), L3 full history (existing assembler). Replaces monolithic memory injection with structured `<memory_palace>` XML blocks.
  - **2-Stage Contradiction Detection**: Embedding distance triage (<0.15 = merge, 0.15—0.35 = LLM-judge, ≥0.35 = temporal close) with Flash-Lite judge verdicts: `update`, `parallel`, or `refinement`.
  - **Persistent Role Diaries**: Each custom role accumulates session insights (key learnings, preferences, style observations) as JSONB entries, automatically injected as L1 context for cross-session continuity.
  - **Real-Time Streaming Extraction**: Every qualifying user message (≥30 chars) fires a background `asyncio.Task` that runs Gemini Structured Outputs (Pydantic schema + `thinking_level="medium"`) to immediately populate the knowledge graph — no more waiting for the 7-day consolidation cycle.
  - **Temporal Conflict Management**: Memory edges have `valid_from`/`valid_to` lifecycle columns. When a user's facts change (e.g., job switch), old edges are closed (`valid_to = now()`) and new ones inserted, preserving complete history. The LLM receives bilingual `<temporal_context>` blocks to celebrate life changes naturally.
  - **RLHF Feedback Cascading**: When a user taps 👎 on a response, the specific graph edges used are penalized, and a negative feedback count cascades to the original `long_term_memory` records. The retrieval engine reads `rlhf_negative_count` and applies a search-time similarity penalty (-0.03 per vote), effectively burying incorrect facts while surfacing better memories. The bot pre-places 👍 /👎 reactions on its own messages as a silent invitation for feedback.
  - **Agentic RAG**: The agentic research loop (`??` prefix) gains a `recall_memory` tool declaration that lets the LLM proactively query the user's personal knowledge graph during research, grounding web answers in user-specific context.
  - **Multimodal Memory**: Image and audio messages trigger background graph extraction after transcription/description, with `file_id`/`file_type` stored on memory nodes for future media re-delivery.
  - **Group Chat Social Graph**: Group messages fire social graph extraction attributed to the specific speaker (`actor_user_id`) within group context (`chat_id`). Privacy isolation via `is_public` flag and RLS-scoped queries.
  - **Knowledge Graph Visualization API**: Mini App endpoint (`/webapp/api/graph`) serves nodes and edges as JSON for interactive visualization with optional query-based filtering.
- **Distributed Concurrency**: Multi-tier Redis-backed global semaphores (heavy and ultra-heavy limits) to prevent API quota starvation in multi-replica deployments while guaranteeing isolation between standard queries and intensive Agentic research loops.
- **Resilient Operations**: Instance-based background task manager with exponential backoff, bare-coroutine safety guard, and admin alerting hooks. Atomic metrics persistence with delta-based increments prevents data loss on restart. Prompt registry validates required variables at render time to prevent silent placeholder leaks.
- **Thinking Level Control**: Configurable reasoning depth for supported models.
- **Adaptive Thinking Budget**: Automatic `thinking_level` selection via 14 regex heuristics + context-aware escalation + **model-aware defaults**. Simple greetings get `low`, code/math/multi-step queries get `high`. `gemini-3.1-flash-lite` defaults to `high` thinking in auto mode for optimal reasoning quality. User explicit preference always overrides.
- **Conversation Branching**: Fork current chat into a "what-if" branch via snapshot. Explore alternative conversation paths without losing the main thread. One-click restore to the original context.
- **Smart Context Window**: Model-specific token budgets (flash-lite: 32K, flash: 128K — evidence-based on context degradation research) with automatic context trimming and LLM-backed summarization of dropped history.
- **Agentic Smart Reminders**: DB-persisted user reminders (`/remind 30m Check logs`) with 60s poll-based delivery via `job_queue`. Supports **Zero-Latency Intent Classification** (automatically detects whether a prompt requires a simple text notification, quick QnA search, or deep agentic research). AI tasks run in non-blocking background tasks (`asyncio.create_task`) with concurrency semaphores (max 3), 5-minute timeout guards, and inline ⚙️ cancel buttons in the reminder list.
- **Context Summarization**: Automatic token compression for large chats via `app/context/` subsystem — `ContextAssembler` orchestrates history assembly within model-specific token budgets, `Summarizer` produces LLM-backed compressed summaries, and `TokenBudget` maps model patterns to limits (flash-lite: 32K, flash: 128K).
- **Document Chunking**: Retrieval-time chunking (`app/documents/chunking.py`) with three strategies — recursive (paragraph/sentence/word), hierarchical (parent/child), and query-aware relevance scoring (`chunk_for_context`) — replacing na️ve hard-truncation.
- **Intelligence Briefs**: DB-persisted topic subscriptions (`/subscribe`, `/unsubscribe`). Hourly job extracts topics from LTM → Tavily search → Gemini summary → Telegram delivery. Backed by `brief_subscriptions` table with RLS.
- **Telegram Mini App**: Native in-app settings panel served as a Quart Blueprint (`/webapp/`). Three-tab interface: **⚙️  Settings Editor** (system prompt, model, thinking level, LTM/search toggles with adaptive density for desktop precision), **🕸️  Knowledge Graph** (interactive force-directed graph canvas with drag, mouse-wheel pivot zoom, and top-left quick-access nav), and **🧠 LTM Explorer** (paginated memory browser with swipe-to-delete and desktop hover-reveal trash button, search, usage stats). Full cross-platform UX: **floating glassmorphic dock** tab-bar on desktop, wheel-redirected horizontal chip scroll with animated arrows and gradient masks, adaptive context-reset button (double-click on pointer devices, hold-to-confirm on touch), `overscroll-behavior` isolation to prevent Telegram close gestures, and `keepalive` fetch for guaranteed memory deletion even when the app is closed mid-undo-window. Tab-bar navigation with haptic feedback. Authenticated via Telegram `initData` HMAC-SHA256. Styled with Telegram theme variables for automatic dark/light mode. Accessible via `WebAppInfo` button in `/settings` command. Uses `WEBHOOK_URL` env var for multi-deployment portability.
- **Crocodile Mini App Game**: 1-on-1 charades game launched from inline mode (`@bot Крокодил`). Player A is shown the hidden word; Player B guesses via a Telegram Mini App WebSocket session. **How to start:** `@bot Крокодил` (random word), `@bot Крокодил:Животные` (category), `@bot Крокодил:=custom` (custom word, known only to creator A). A **4-tier semantic judge** evaluates each guess: Levenshtein exact match → 24h Redis judgement cache → Race×3 LLM (`gemini-3.1-pro` + `gemini-2.5-flash-lite` fallback) → hardcoded no-match fallback. "Midnight Glass" UI features a volumetric glassmorphism design with ambient depth, floating category pills, interactive chat bubbles with 3D temperature auras (🧊/🤔/🔥/🎯), dynamic pulsing hints, and a floating tactile input island.
  - **Spectator Mode (God Mode):** Creators who start games using a custom word are placed into an interactive Spectator Mode. They cannot guess, but they receive a real-time synchronized feed of the guesser's chat bubbles. The UI features a fixed Target Word Banner, live typing indicators, and a persistent Reaction Bar allowing the creator to send live emojis (`🔥`, `ℹ️ `, `😂`, `👍 `, `🤔`) that fade into the guesser's chat stream. Driven by an in-memory PubSub system (`asyncio.Queue` based) decoupling events from socket loops.
  - **Instant Game Start & Async Generators (Bug 6.3/6.4):** Custom words (=крокодил) instantly assign the static category Слово игрока (особое), eliminating up to 15s of blocking LLM category classification delays. Randomly chosen games eliminate initial starvation delays by generating a single word fast (Vertex AI Express primary, `OPENCODE_INLINE_MODEL` fallback) only if the candidate passes a strict lexical validator, so malformed fast-path garbage never seeds the cache. The full 20-word bank fill is delegated to a bounded background queue instead of an unbounded fire-and-forget task.
  - **Topic Canonicalization (v2.15.12):** Crocodile category text now resolves through `resolve_topic()` into a stable `topic_id` before word selection and cache lookups. Normalized aliases share the same pool (for example, `герой League of Legends` and `герои Лиги Легенд` map to one canonical topic), preventing fragmented banks caused by spelling variants.
  - **Non-Repeating Topic Rotation (v2.15.12):** Topic starts no longer repeatedly return the same first word. `pick_random_word_for_topic()` applies a per-topic rotation cursor over the active bank and resets automatically only when the bank content hash changes.
  - **Topic-Aware Judge Context & Cache Isolation (v2.15.12):** Judge requests now include `category`, `topic_id`, and `sense_context`, and judgement/hints/generated-word caches are keyed by this context. This prevents cross-domain meaning drift (for example, champion `Nocturne` vs musical `nocturne`) and removes false cache hits between unrelated topics.
  - **Security hardened (v2.12.8):** WebSocket connections require valid Telegram `initData` HMAC-SHA256 — unauthenticated connections receive `4003 initData required` immediately. Creator Guard protection restricts guessing actions natively, decoupling authorization boundaries. API key material is sanitized from frame locals before race coroutines are spawned. The local fallback game-lock registry is unbounded (~100 bytes/lock × game count = negligible memory); the prior 512-entry FIFO eviction cap was a critical concurrency defect (evicting held locks broke mutual exclusion under Redis outage). Backed by **130+ automated tests** via `pytest-xdist`, mocking 100% of LLM calls, Quart endpoints, and WS pipelines. Requires no new env vars — uses existing `REDIS_URL`, `WEBAPP_BASE_URL`, and `GEMINI_API_KEYS`.
  - **Judge Tolerance (v2.12.9):** `GuessJudgement.hint` `max_length` raised from 80 → 255 characters. Resolves a `Pydantic ValidationError` cascade triggered when the primary model (`gemini-3.1-flash-lite`) returns 503 and the fallback (`gemini-2.5-flash-lite`) generates verbose hints exceeding the old limit.
  - **Emoji Temperature Prefix (v2.14.0):** Every judge response now carries an automatic emoji prefix based on semantic score (`🧊`/<30%, `🟡`/<70%, `🔥`/<92%, `🎉`/≥92%). The AI’s witty hint text is preserved 100%—the temperature is prepended on the backend before the WebSocket event fires.
  - **Inline Message Thermometer (v2.14.0):** After every new best score the bot silently `edit_message_text`s the inline message showing `🔥 Лучшая попытка: [██████░░░░] 60%`. Persisted via `best_score` field in Redis.
  - **Graceful Surrender (v2.14.0):** The guesser can tap a `Сдаться` button. The game ends cleanly with `🏳️ Игрок сдался.` and both players receive a `surrendered` WebSocket event.
  - **Creator “God Mode” Custom Hints (v2.14.0):** A creator can type any hint text in their WebApp — it is relayed instantly to the guesser via the Pub/Sub bus. Zero LLM calls.
  - **12 Word Categories (v2.14.0):** Added `Транспорт`, `Одежда`, `Музыка`, `Космос` (15 words each, bilingual RU+EN) plus 17 new aliases. Word-category LRU cache (`category_cache.json`, 10k entries) means any previously-seen custom word is classified in <1ms.
  - **Local Heuristics Hardening (v2.14.0):** `_homogenize_pair` now maps `ё→е` and strips trailing punctuation before Damerau-Levenshtein matching. A guess of `кот.` or `бобёр` is accepted locally without an LLM call.
  - **Hint Race Hardening (v2.15.11):** Progressive hints now race three independent lanes without aborting on the first provider failure: **1 AI Studio lane** (`gemini-3-flash-preview`), **1 optional Vertex AI Express lane** (`gemini-3.1-flash-lite`), and **1 curated OpenCode Go lane** (prefers `opencode-go/glm-5.1`). Router/bootstrap exceptions are isolated per lane, so a fast failure no longer cancels a slightly slower valid hint response.
  - **Multi-Worker Runtime Sync (v2.15.13):** Crocodile WebSocket broadcasts, reconnect history, and pre-generated per-game hints now flow through a Redis-backed runtime store instead of process-local memory only. Guess mutation is protected by a per-game Redis lock, so multi-worker deployments no longer silently split game state across different Uvicorn/Gunicorn workers.
  - **Redis L1/L2 Crocodile Caches (v2.15.13):** `judgement_cache.py` now keeps hot entries in-process while mirroring judgements, hints, category resolution, and generated-word banks into Redis. JSON files under `app/games/data/` remain as fallback persistence only when Redis is unavailable, eliminating the old multi-worker file-fragmentation problem.
  - **Debounced Inline Thermometer (v2.15.13):** Inline best-score updates are now coalesced through a dedicated Telegram service with a 2-second debounce window. Players still see the same thermometer text, but rapid guess bursts no longer spam `edit_message_text` for every intermediate score.
  - **Generated Word Bank Upgrade Guard (v2.15.13):** Fast-path single-word seeds for new custom topics are treated as provisional only. Before reuse, the backend upgrades them into a full generated bank so custom categories do not get stuck serving one-word pseudo-caches after the first quick start.
  - **Topic-Scoped Pool Isolation (2026-04-22):** Topic-backed custom categories now use one canonical internal storage identity derived from `topic_id`, not raw display text. Similar wording variants still share the same logical pool, while different topics no longer hydrate generated banks or hints through legacy unscoped cache fallbacks. This closes the path where stale or unrelated custom-topic words could leak into a later game.
  - **Safe Provisional Banks + Batched Hint Prewarm (2026-04-22):** Fast-start words are now stored separately from durable generated banks and are dropped as soon as a full bank is available, so a singleton seed cannot survive as the topic's long-term pool. Background bank hint warming can now batch multiple words in one request, but every response is validated per requested word and any missing/mismatched entry falls back to the existing single-word hint generator, preventing cross-word hint contamination.
  - **AI Load-Shedding & Hint Backpressure (2026-04-22):** Added a Crocodile-only AI budget coordinator with foreground/background lanes, Gemini retry-after model cooldowns, bounded bank hint prewarm (1 worker, up to 2 words), and topic-aware hint singleflight dedupe. Live gameplay traffic now takes priority over cache warming, preventing minute-limit bursts and duplicate hint races during custom-topic sessions.
  - **Daily Crocodile Dual Track & Delivery Hardening (2026-04-22):** `/dailycroc` and scheduled prompts now open through a `t.me/<bot>/<miniapp>?startapp=daily` deep link, so Telegram always injects `initData` and the Mini App WS auth layer stays valid. The daily experience now exposes two independent prepared tracks, **Easy** and **Hard**; finishing one mode never hides the other. Scheduled delivery remains opt-in, sends only once per user's **local day** after the preferred local hour, and respects automatically captured Mini App timezone data.
  - **Guaranteed Daily Completion Summary (2026-04-22):** Finished daily games always send a result body with score, rank, streak, leaderboard snapshot, mode status summary, and a CTA to the remaining daily track if it is still available. Result messages are tracked in PostgreSQL and refreshed through a debounced background editor whenever new global scores arrive, avoiding Telegram edit storms while keeping the visible leaderboard current.
  - **Daily Delivery Control & Preparation Window (v2.15.15):** Admins can toggle outward daily delivery at runtime via `/set_dailycroc_delivery on|off` without disabling puzzle preparation. The scheduler now prepares a forward window of daily puzzles in advance and only sends a prompt once today's puzzle is fully ready.
  - **Prepared Daily Assets & Non-Repeating Words (v2.15.15):** Daily puzzle preparation now reserves words against the persisted `crocodile_daily_puzzles` history, pre-generates hints, stores an image prompt, and requests completion art ahead of time through Pollinations `qwen-image` with prompt enhancement. This avoids day-of-send failures where hints or the art payload would otherwise still be pending.
  - **Daily Completion Art (v2.15.15):** When a player finishes the daily puzzle, the bot can send the pre-generated completion illustration before the result message, preserving the score/rank/streak flow while adding a ready-made reveal asset instead of generating on the critical path.
  - **Single-Message Daily Completion Fallback (2026-04-23):** If the original prompt photo can no longer be upgraded in place, the fallback path now prefers one Telegram photo result card with the prepared completion art attached and the score/rank/leaderboard rendered in the caption, instead of splitting completion into two back-to-back messages.
  - **Daily Delivery Resilience & Image Best-Effort (2026-04-22):** `is_puzzle_fully_prepared` now only requires hints — Pollinations image generation is best-effort and retried hourly by the scheduler without blocking player delivery. The scheduler now fires `alert_admin(CRITICAL)` if `ensure_prepared_puzzles` throws, and `alert_admin(WARNING)` when a puzzle is missing for today, so ops sees failures in Telegram immediately.
  - **Daily Ops Snapshot & Smoke-Test Controls (2026-04-23):** `/dailycroc_status` is now an interactive operator card instead of a static dump. It exposes in-place `Refresh`, `Prep check`, and `Send test to admin` controls, shows per-difficulty readiness as separate `puzzle / hints / art / prepared_at` components, and records the last placeholder smoke-test result (`photo` vs `text`, plus timestamp) in `global_settings` so admins can verify that a saved placeholder still survives the real Telegram `send_photo` path.
  - **No-Op Daily Status Refresh Handling (2026-04-23):** Pressing `Refresh` or `Prep check` on `/dailycroc_status` no longer surfaces a false operator error when Telegram returns `Message is not modified` for an identical card. Unchanged snapshots are now treated as a successful no-op instead of a failed refresh.
  - **Test-Send Contract Isolation (2026-04-23):** The admin-only daily smoke test reuses the real `send_daily_prompt()` transport but explicitly skips delivery bookkeeping and prompt-message tracking. Test sends therefore validate the production invite path without polluting `last_sent_*` delivery state or creating fake prompt records that could later interfere with prompt-to-art swaps.
  - **Leaderboard Display Names (2026-04-22):** Each user's Telegram `first_name [+ last_name]` is extracted from `initData` on first WebSocket connect and upserted into `public.users.display_name` (migration 044). Both the Mini App overlay leaderboard and the Telegram result message now show real names instead of masked numeric IDs, falling back to `игрок <last 4 digits>` for users who haven't opened the Mini App yet.
  - **Category Pill Layout Fix (2026-04-22):** `#category-pill` converted from `position: absolute; top: 60px` (overlapped the daily-modes chip row when it was visible) to an in-flow `align-self: center` flex item with `display: none` until JS sets its text. `#chat-area` top-padding reduced from 40px to 6px accordingly.
  - **Multi-Worker Prep Lock Hardening (2026-04-22):** `prepare_daily_puzzle()` now acquires a Redis distributed lock (`daily:prep:{date}:{difficulty}`, 60s TTL, 30s blocking) alongside the per-process `asyncio.Lock` fast-path. If another worker holds the Redis lock, the function immediately loads the existing puzzle from the DB instead of duplicating LLM + Pollinations work. The in-process `_PREP_LOCKS` registry is unbounded (no eviction) — memory is negligible (~100 bytes/lock × 730 entries/year), and the prior FIFO eviction cap was a race condition hazard because actively-held locks could be evicted while awaited.
  - **Distributed Mutation Lock Hardening (2026-04-22):** `game_mutation_lock` in `crocodile_runtime.py` now raises `TimeoutError` on Redis lock contention instead of silently falling back to a local `asyncio.Lock`. WebSocket routes (`daily_game_ws`, `game_ws`) catch `TimeoutError` gracefully and return structured JSON (`{"event": "error", "message": "Сервер загружен..."}`) so the Mini App can surface a retry prompt instead of crashing with 1011.
  - **Thread-Safe Cache Writes (2026-04-22):** All four JSON cache `_persist_sync` helpers in `judgement_cache.py` now hold dedicated `threading.Lock` objects per cache file. Concurrent `asyncio.to_thread` dispatches are serialized before the atomic `.json.tmp` → rename step, eliminating file corruption races on Windows (and any OS where file rename is not atomic across threads).
  - **Creator God-Mode Reconnect (2026-04-22):** The word-giver (creator) can now reopen the Mini App after the game has ended and see the full result. Previously `game.status != 'active'` closed the WebSocket for everyone including the creator with `4009`. Now: non-creators still get `4009`; the creator receives a `game_state` snapshot (with `target_word`, `finished: True`, `status`) + `history_sync`, then the socket closes gracefully with `1000`, allowing the Mini App to render the post-game overlay.
- **Live Audio Voice Chat (Gemini Live API + Experimental Vertex Route)**: Real-time bidirectional voice conversation with AI via a Telegram Mini App (`/webapp/live`). The **default route** uses **`gemini-3.1-flash-live-preview` via the Gemini GenAI Live API** for sub-second duplex audio streaming over `/webapp/live/ws`. An **opt-in experimental route** is also available through **Vertex Live** on **`gemini-live-2.5-flash-native-audio`** over `/webapp/live-vertex/ws`; this mode is exposed in the Mini App settings as **`Vertex Live · с доступом в интернет`** and enables **Google Search grounding** for the live session. **Architecture**: Browser captures mic audio via **AudioWorklet** (PCM16, 16kHz mono, 100ms chunks), sends base64-encoded frames over WebSocket to a **Quart WebSocket proxy**, which bridges to the selected live backend. Audio responses (PCM 24kHz) are relayed back and played via Web Audio API. **Features & Hardening**: explicit **push-to-talk** turn boundaries (`activity_start` / `activity_end`) instead of relying on implicit browser-side pauses, real-time **input/output audio transcription** in the transcript pane, context-window compression, session resumption handles, interruption handling (playback queue flush on `interrupted` signal), circular waveform visualizer with frequency-reactive bars, turn-based receive handling to avoid per-response reconnect churn, and 10-minute idle timeout. The Mini App now waits for the live session to be ready before it starts the microphone capture path, which eliminates the earlier race where the first short utterance could be dropped while the socket was still opening. Live preferences are **separate from reply-TTS voice settings** and are configured inside the Live Mini App itself: users can pick a live connection mode, choose a Gemini live voice, select a simple live-thinking preset (`Быстрый` / `Сбалансированный` / `Умный`), and browse voices grouped as **женские / мужские**. Changes during an active call are applied via a controlled reconnect, preserving the transcript UI while re-opening the session with the new config. If the experimental Vertex route fails during connect/setup, the Mini App performs a **single-session fallback** to the standard GenAI live route without rewriting the saved user preference. Vertex Live expects a **regional Vertex client plus readable ADC credentials inside the bot container** (`GOOGLE_APPLICATION_CREDENTIALS` pointing to a mounted service-account JSON); an unreadable credentials file is treated as a controlled misconfiguration instead of surfacing a raw session crash. Server logs also distinguish the live backend explicitly (`via=vertex_live` vs `via=gemini_live`) so the active transport is visible during debugging. Session traffic is strictly capped to 1 active socket per user to prevent overlapping API drains. Includes seamless UI haptics (`Telegram.WebApp.HapticFeedback`) across state transitions. Authenticated via Telegram `initData` HMAC-SHA256. Covered by automated unit and E2E WebApp socket tests.
  - **Provider Boundary:** Live Audio now has two explicit boundaries: **standard live** on the Gemini GenAI Live API for the stable deployed voice flow, and an **experimental Vertex Live internet-enabled route** for targeted testing. Inline/search/Crocodile workloads remain independent of this selector.
- **Smart UX Interactions**: RLHF feedback via a two-stage "📔  Оценить" inline toggle that expands into 👍 /👎 choices to reduce UI clutter. Citation badge `[📚 N фактов (interactive)]` shows when memory was used — tapping it displays an alert with the exact graph relationships and sources used to generate the response. Smart LLM-generated suggestions (`[SUGGESTIONS:...]` tags → inline buttons via memory-cached hash identifiers to bypass Telegram's data limits), intent routing (`[INTENT:...]` → contextual actions), `CopyTextButton` for code blocks, `sendMessageDraft` for input pre-filling, and `🔥` message effects for image generation.
- **Native Mini App Reader (SSR Architecture)**: High-performance delivery for long responses (>4000 chars). Content is instantly saved to Redis and rendered inside a **Telegram Mini App**. The reader now uses **Server-Side Rendering (SSR)**: Markdown→HTML conversion, TOC extraction, and Bionic Reading transforms are performed on the server (`app/utils/reader_utils.py`) before the page is sent — achieving instant FCP with no skeleton loading state. **Cold-Storage Reverse-Proxy**: When the 24h Redis TTL expires, the reader transparently fetches and parses the associated Telegraph page, serving it through our own UI instead of redirecting. Graceful fallback chain: Redis hit → Telegraph proxy → Telegraph link redirect. **UX features**: floating TOC FAB (bottom sheet, swipe-to-close, haptic feedback), full-screen code modal with syntax re-highlighting, 20+ language file download, Bionic Reading toggle (word-stem bolding, `sessionStorage` persistence), and browser TTS ("🔊 Вслух" / "⏹ Стоп"). Code blocks include a one-click copy button (✓ confirmation), gradient overflow indicators, and file download support.
- **Auto TTS for Research**: Fire-and-forget voice synthesis of research results via ElevenLabs, triggered automatically after successful agentic search responses.
- **Administrative Dashboard**: Quart-based web server serving Prometheus metrics (`/metrics`), system health overviews, batch API (`/api/dashboard` — 8 metrics in 1 RTT), SSE live updates (`/api/events` — 5s real-time stream), and key health diagnostics (`/api/key-health`). Frontend integrates SSE EventSource for real-time CPU/memory/queue updates between polls.
- **Request Deduplication & Debouncing**: In-memory double-tap prevention middleware with 3s window and MD5 hashing blocks duplicate identical requests. Rapid-fire text messages and grouped forwards are handled by a **1.1s Trailing Message Debounce** aggregation window. The timer resets on every incoming fragment, flawlessly merging long bursts of messages into a single cohesive AI context before processing.
- **Key Rotation Observability**: Structured `KEY_EVENT` logging for usage milestones, near-limit warnings (70%), threshold rotations, and a `get_health_summary()` dashboard API with per-key status snapshots. **Game Judge Key Rotation (v2.12.10):** the Crocodile judge `_one_call` now classifies each API exception via `classify_key_error()` and fires `KeyHealthRepository.suspend_key()` as a background task — `quota` errors (429 RESOURCE_EXHAUSTED) suspend the key until midnight PT, `transient` errors (503) for 15 s, `permanent` errors indefinitely. `resolve_ai_request()` already filters suspended keys at SQL level, so the next race round automatically receives a fresh key. All judge call attempts are recorded via `record_api_call("gemini_judge")` and overall latency via `record_request("judge", elapsed, success)`. Full `record_api_call` / `record_request` instrumentation also added for `gemini_chat`, `gemini_transcribe`, and `gemini_vision` pipelines (v2.12.10).
- **Dynamic Key Management**: Centralized `/keys` admin wizard providing an inline keyboard UI to securely view, edit, and clear API provider keys (Weather, Exchange, Gemini, etc.) at runtime without restarting the application. Includes automatic 30-min background health checks pointing to provider verification endpoints, alerting the admin via Telegram if a provider fails.
- **Structured Error Classification**: O(1) type-based error classification via `ErrorCode` enum (17 exception types + 8 HTTP status codes), replacing fragile emoji/text pattern matching. Full error-to-user-message mapping.
- **Graceful Shutdown**: Two-phase drain (pending state persists + task queue) before resource cleanup, preventing data loss during deploys. Includes shutdown hooks for `intent_router` HTTP clients and background task managers.
- **Intent Direct Routing**: Lightweight API bypass for simple utility queries — intercepted before hitting the LLM, providing near-instant responses even during complete API outages. **Weather** via WeatherAPI.com (single request: geocoding + forecast + localized Russian conditions + "feels like" temperature; graceful fallback to Open-Meteo when API key is absent). **Fiat currency** via ExchangeRate-API v6 (supports RUB/KZT/UAH/KGS/UZS; 1,500 req/month free tier; fallback to Frankfurter for EU pairs). **Crypto** via CoinGecko Demo API (keyless, 30 rpm; BTC/ETH/SOL/TON with Russian aliases like "биткоин", "сфир", "тон"; shows USD + RUB price and 24h change). Multi-day/temporal weather queries ("завтра", "вечером") are routed to the LLM with Google Search Grounding instead of raw API calls. Russian locative/prepositional case suffixes are stripped before geocoding ("Саратове" → "Саратов").
- **Live WS Capacity Handling**: The standard Gemini Live Audio WebSocket handler uses the Gemini GenAI Live API path and surfaces controlled capacity/misconfiguration fatal events instead of attempting AI Studio key rotation. The experimental Vertex internet-live route exposes the same client event contract and falls back once to the standard route only when Vertex fails during connect/setup. Classic Telegram-side retry resilience remains unchanged.
  - **Streaming Reliability**: Exponential backoff retry (0.5→1→2s + jitter) for Telegram rate-limit errors with adaptive debounce escalation (auto-scales up to 3s).
- **Security & GDPR**: CSRF-protected dashboard authentication, brute-force rate limiting (60 req/min/IP on all API endpoints), API key masking in status endpoints, and Telegram commands for data export (`/mydata`) and deletion (`/deleteme`).
- **Cold-Start Latency Optimization**: Systematic import-time profiling (`-X importtime`) identified four heavy module chains loaded eagerly at startup. Three targeted lazy-import refactors reduced P95 cold-start time by **~54%** (from ~2.41 s to ~1.11 s measured via `artifacts/perf/bench_startup.py` across 5 subprocess spawns): (1) `app.handlers.messages` — deferred `cmd_image` imports inside the task wrapper; (2) `app.handlers.msg_roles` — deferred `app.agents` inside `handle_custom_role_generation`; (3) `app.handlers.menus` — deferred `app.document_processor` (pulls `pypdf` + `docx`) into the two async call sites; (4) `app.handlers.cmd_admin` — deferred `google.genai` SDK into `list_models_command` (admin-only, rarely called). Python's module cache (`sys.modules`) ensures each deferred import is free after the first call.

## Non-Goals / Limitations

- **Voice Processing Limitations**: Voice transcription uses `gemini-3.1-flash-lite` — quality depends on audio clarity and language support of the underlying model. Conversational voice flow requires user confirmation before AI processing.
- **OpenRouter Limitations**: Multimodal detection (images) strictly forces Gemini; OpenRouter is not utilized for vision tasks. **Exception**: Opencode `mimo-v2-omni` natively supports `image_url` in messages and is exempt from the Gemini-only vision redirect.
- **Local Rate Limits**: Heavy request limits are rigidly enforced per user to prevent API quota drain (`MAX_CONCURRENT_HEAVY_REQUESTS`).
- **No ORM**: Raw SQL via asyncpg; no SQLAlchemy or Alembic.

## Architecture

- **Bot Container (`tg-bot`)**: A single async event loop runs both the Telegram webhook updater and the Quart web server via Hypercorn. Webhook mode: Quart receives `POST /webhook/<token>`, deserializes `Update`, and passes it to PTB's internal `concurrent_updates(50)` queue — the HTTP response returns `200` immediately while processing runs asynchronously. Falls back to long-polling if `WEBHOOK_URL` is unset.
- **Local Bot API Server (`tg-api`)**: Self-hosted `telegram-bot-api` container (`aiogram/telegram-bot-api:latest`) communicates with Telegram via MTProto. The bot sends HTTP requests to `http://tg-api:8081/bot` instead of `api.telegram.org`. Shared Docker volume (`tg-api-data`) enables zero-copy file access for voice/photo/document processing. File limit: 2 GB (vs 50 MB cloud API). Timezone: `Europe/Kyiv`.
- **Media Cleanup Cron (`tg-media-cleanup`)**: Alpine-based sidecar container that runs a 60s-tick loop: (1) `chmod -R g+rX` on the shared volume to fix permission conflicts between `telegram-bot-api` (UID 101) and the bot container (GID 101), and (2) deletes cached media files older than 7 days every 24 hours to prevent disk exhaustion.
- **Database (PostgreSQL)**: Source of truth for users, chats, messages, metrics, roles, and pgvector embeddings.
- **Cache (Redis)**: Optional high-speed layer for caching rate limits and transient states.
- **Third-Party APIs**: Google Gemini (native SDK), Opencode Go (HTTPX), OpenRouter (HTTPX), JINA AI (HTTPX), Tavily (HTTPX).

> **Docker networking:** All three containers share a `tg-net` bridge network. The shared volume `tg-api-data` is mounted at `/var/lib/telegram-bot-api` in both `tg-api` and `tg-bot`. The bot's web server binds to `127.0.0.1:$PORT` on the host (not `0.0.0.0`) — Caddy/Nginx reverse proxy is expected in front.

```mermaid
graph TD;
    User-->TelegramCloud[Telegram Cloud];
    TelegramCloud-->LocalAPI["tg-api<br/>Local Bot API Server<br/>MTProto ↔ REST"];
    LocalAPI-->BotHandler["tg-bot<br/>Python Bot + Quart"];
    Admin-->QuartServer["Quart Web Server<br/>/metrics, /health, /webapp"];

    BotHandler-->ProviderRouter;
    ProviderRouter-->Gemini[Google Gemini];
    ProviderRouter-->OpencodeGo[Opencode Go];
    ProviderRouter-->OpenRouter[OpenRouter];

    BotHandler-->Tavily[Tavily Search];

    BotHandler-->Cache[(Redis)];
    BotHandler-->DB[(PostgreSQL/pgvector)];
    QuartServer-->DB;

    LocalAPI-.->SharedVolume["tg-api-data<br/>/var/lib/telegram-bot-api<br/>Shared Docker Volume"];
    BotHandler-.->SharedVolume;
    Cleanup["tg-media-cleanup<br/>Alpine cron<br/>chmod + 7d prune"]-.->SharedVolume;
```

## Repository Structure

| Path                  | Purpose                                                                        |
| --------------------- | ------------------------------------------------------------------------------ |
| `app/`                | Core application logic (bot, web server, DB layer, handlers).                  |
| `app/handlers/`       | Telegram command and message processors (`ai_chat`, `ai_search`, `commands`, `inline`). |
| `app/repos/`          | Database repository pattern implementations (queries for chats, memory, keys). |
| `app/providers/`      | AI provider abstraction layer (base, Gemini, OpenRouter, router with race requests). |
| `app/core/`           | Agentic research engine — multi-step query decomposition and tool use.         |
| `app/context/`        | Context assembly subsystem (assembler, summarizer, token budget).              |
| `app/documents/`      | Document processing: chunking strategies, parsers, document repository.        |
| `app/middleware/`     | Request pipeline middleware (dedup).                                           |
| `app/adapters/`       | Concurrency primitives and Telegram UI adapter.                                |
| `app/db/`             | Database bootstrap: schema validation, migrations runner, RLS, seed.           |
| `app/utils/`          | Shared utilities (formatting, keyboards, background tasks, image utils, reader SSR, etc.). |
| `app/templates/`      | HTML Jinja2 templates for admin dashboard and Telegram Mini App.               |
| `app/games/`          | Crocodile (Charades) game engine: state machine, semantic judge, word bank, judgement cache. |
| `app/bot_instance.py` | PTB Bot singleton — allows non-PTB code (WebSocket handlers) to call Bot API methods. |
| `app/web_miniapp.py`  | Quart Blueprint for Telegram Mini App (initData auth, memory/settings/game API). |
| `app/deferred_response.py` | Redis-backed deferred AI generation worker for background retry after total outage. |
| `app/intent_router.py`| Lightweight LLM-bypass for weather/currency/crypto queries via WeatherAPI.com, ExchangeRate-API & CoinGecko. |
| `app/state.py`        | In-memory user state management with TTFB stall tracking for network recovery. |
| `docs/`               | Extended architectural documentation.                                          |
| `scripts/migrations/` | Numbered SQL migration files — single source of truth for all DDL.             |
| `tests/`              | Comprehensive test suite (Unit and Integration).                               |
| `bot.py`              | Main application entry point uniting Quart and the Telegram updater.           |

## Tech Stack

| Layer           | Technology            | Purpose                                        |
| --------------- | --------------------- | ---------------------------------------------- |
| Runtime         | Python 3.14-slim      | Execution environment                          |
| Bot Framework   | `python-telegram-bot` | Async interaction with Telegram APIs           |
| Web Server      | Quart + Hypercorn     | Lightweight dashboard & Prometheus `/metrics`  |
| Database        | `asyncpg`             | High-performance Async PostgreSQL driver       |
| Vector DB       | `pgvector`            | Storing and querying semantic memories         |
| Data Validation | `pydantic`            | Configuration and strictly-typed object models |
| Observability   | `structlog`           | Structured JSON logging & context correlation  |

## Setup

1. Clone the repository.
2. Ensure Python 3.14-slim and PostgreSQL (with `pgvector` extension) are installed.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` (if applicable) and fill in necessary configuration.
5. Create PostgreSQL database with `pgvector` extension. Pending numbered SQL migrations from `scripts/migrations/` are applied automatically on startup; migration files are kept idempotent so fresh deploys and repeated bootstrap runs remain safe.

## Configuration

All configuration is loaded from environment variables (or a `.env` file). Variables are grouped below by functional category.

> [!IMPORTANT]
> Variables marked ✅ are **required** — the application will refuse to start if they are absent. Variables marked ⚙️ are optional and will use the listed defaults.

---

### 📔  Core / Authentication

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` | — | Obtained from [@BotFather](https://t.me/BotFather). Must match the running bot. |
| `ADMIN_ID` | ✅ | `6913772015` | — | Your personal Telegram User ID. Get it via [@userinfobot](https://t.me/userinfobot). Used to gate all `/admin` commands. |
| `ADMIN_SECRET` | ⚙️ | Any secure random string, e.g. `openssl rand -hex 24` | — | Password for the admin web dashboard login form. Also used as encryption seed for stored API keys in the DB. **Keep stable across restarts** — changing it breaks decryption of stored keys. |

---

### 🗄️  Database

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://user:pass@localhost:5432/gemaibotv2` | — | Standard asyncpg/libpq DSN. **Must** have the `pgvector` extension available — the bot won't start without it. For local VPS deployments, use `localhost`; for Supabase use the pooler URL. |
| `DB_POOL_MIN_SIZE` | ⚙️ | `2`—`10` | `2` | Minimum open asyncpg connections. Increase on high-traffic deployments to avoid connection storms. |
| `DB_POOL_MAX_SIZE` | ⚙️ | `10`—`50` | `10` | Maximum open asyncpg connections. Keep below the PostgreSQL `max_connections` limit (default 100). For a 4-vCPU VPS, `20`—`30` is a safe value. |
| `TEST_DATABASE_URL` | ⚙️ | Same DSN format, pointing to a test DB | — | Used **only** during integration test execution (`pytest -m integration`). Completely isolated from production data. |

> **Migrating from Supabase to a local DB:**
> ```bash
> # 1. Dump from Supabase
> pg_dump "postgresql://postgres.xxx:PASS@aws-1-eu-north-1.pooler.supabase.com:5432/postgres" \
>   --no-owner --no-acl -Fc -f backup.dump
>
> # 2. Create local DB with pgvector
> createdb -U postgres gemaibotv2
> psql -U postgres gemaibotv2 -c "CREATE EXTENSION IF NOT EXISTS vector;"
>
> # 3. Restore data
> pg_restore -h localhost -U postgres -d gemaibotv2 --no-owner --no-acl -Fc backup.dump
> ```
> After restore, update `DATABASE_URL` to `postgresql://postgres:pass@localhost:5432/gemaibotv2` and redeploy. **No data is lost.**

---

### 📦 Cache & Queue (Redis)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `REDIS_URL` | ⚙️ | `redis://localhost:6379/0` or `rediss://user:pass@host:6379` | — | Used for: distributed LLM semaphores (multi-replica safety), Telegram Mini App Reader page cache (24h TTL), Imagen RPD-per-key counters. If absent, the app falls back to in-process locking (works fine for single-replica deployments). Use `rediss://` scheme for TLS connections. |

---

### 🌐  Network & Web Server

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `PORT` | ⚙️ | `10000` | `10000` | Port the Quart web server and admin dashboard binds to. On VPS, expose via Caddy/Nginx reverse proxy — do **not** bind directly to `0.0.0.0` in production. |
| `ENABLE_WEB_SERVER` | ⚙️ | `true` / `false` | `true` | Disabling this skips starting the Quart server entirely. Set `false` only for local dev without the dashboard. |
| `WEBHOOK_URL` | ⚙️ | `https://bot.example.com` | — | If set, the bot registers itself as a Telegram Webhook at this URL and stops long-polling. **Must be HTTPS.** Required for production webhook deployments. If absent, the bot uses long-polling (simpler for single-server setups). |
| `WEBAPP_BASE_URL` | ⚙️ | `https://bot.example.com` | `""` | Public URL from which the Telegram Mini App settings panel and reader are served. If empty, long-response reader falls back to Telegraph links. Must equal `WEBHOOK_URL` in most deployments. |
| `MINIAPP_SHORT_NAME` | No | `gemaibotv2` | `""` | Short name for the Telegram Mini App deep links (`t.me/<bot>/<short_name>`). Set via BotFather: Edit Bot -> Edit MenuButton. |

---

### 🖥️  Local Bot API Server (Optional)

When configured, the bot communicates with a self-hosted Local Bot API Server instead of `api.telegram.org`. This eliminates network latency for file operations, enables 2 GB file uploads, and provides zero-copy media access via a shared Docker volume.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TELEGRAM_API_ID` | ⚠️  | `12345678` | — | From [my.telegram.org](https://my.telegram.org). Required only when running the Local Bot API Server container. |
| `TELEGRAM_API_HASH` | ⚠️  | `0123456789abcdef...` | — | From [my.telegram.org](https://my.telegram.org). Required only when running the Local Bot API Server container. |
| `TELEGRAM_LOCAL_SERVER_URL` | ⚙️ | `http://tg-api:8081/bot` | `""` | URL of the Local Bot API Server. When set, enables `local_mode=True` in PTB. When empty (default), the bot uses the official Telegram cloud API. |

---

### 🤖 Gemini Models (Primary LLM Provider)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `GEMINI_API_KEYS` | ✅ | `key1,key2,key3` | — | Comma-separated Google AI Studio API keys. The system rotates through them automatically on quota exhaustion or 503 errors for Gemini chat/judge/fallback paths. Each key has an independent daily request budget tracked in the DB. Minimum 1 key required. Live Audio no longer consumes this pool. |
| `GEMINI_AVAILABLE_MODELS` | ⚙️ | `gemini-2.5-flash,gemini-3.1-flash-lite` | See `config.py` | Controls which Gemini models appear in the `/model` selector for users. If a `DEFAULT_MODEL` is not in this list, it is added automatically with a warning at startup. |
| `DEFAULT_MODEL` | ⚙️ | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` | Model used for standard conversational messages. Recommended: `gemini-3.1-flash-lite` (fast + cheap) or `gemini-2.5-flash` (smarter, higher cost). |
| `QNA_MODEL` | ⚙️ | `gemini-2.5-flash-lite` | `gemini-2.5-flash-lite` | Model used for quick Q&A web search queries (`?` prefix). Optimized for fast factual one-shot answers. |
| `RESEARCH_MODEL` | ⚙️ | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` | Model used for synthesizing Tavily search results into a final research answer. |
| `URL_SELECTION_MODEL` | ⚙️ | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` | Lightweight model that scores and filters candidate URLs during agentic web research before full content extraction. |
| `TAXONOMY_MODEL` | ⚙️ | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` | Model used by MemPalace to classify memories into the Wing/Room taxonomy and to judge temporal contradictions (LLM-as-Judge). Can be set to a cheaper model without quality loss. |

---


---

### Vertex AI (Primary Inline Driver + Optional Live Internet Route)

When `VERTEX_AI_KEY` and `VERTEX_AI_PROJECT` are set, Vertex AI Express is used as the **primary slot** in the inline race, the primary fast-path word-generation slot for Crocodile, and the primary background hint-prewarm lane. **Live Audio remains separate by default**: the stable deployed Mini App voice flow still runs through the Gemini GenAI Live API path (`gemini-3.1-flash-live-preview`). For testing, the Live Mini App now also exposes an **opt-in Vertex Live route** on `gemini-live-2.5-flash-native-audio` with Google Search grounding, selectable as `Vertex Live · с доступом в интернет`.

For that experimental live route, `VERTEX_AI_PROJECT` and `VERTEX_AI_LOCATION` are not enough by themselves. The bot container must also receive readable ADC credentials, typically by mounting a service-account JSON and exporting `GOOGLE_APPLICATION_CREDENTIALS` to that in-container path. The included GitHub Actions deploy flow supports this via the `VERTEX_LIVE_SERVICE_ACCOUNT_JSON` secret and mounts it read-only into the bot container.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `VERTEX_AI_KEY` | No | GCP API key string | `""` | GCP API key for a service account with Vertex AI API access. Tried after AI Studio key exhaustion when `VERTEX_AI_PROJECT` is also set. |
| `VERTEX_AI_PROJECT` | No | `my-gcp-project-123` | `""` | GCP project ID where Vertex AI API is enabled. Required together with `VERTEX_AI_KEY`. |
| `VERTEX_AI_LOCATION` | No | `us-central1` | `us-central1` | Vertex AI region. Must match where your models are available. |

### ?? Opencode Go Models (Primary LLM Provider)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `OPENCODE_API_KEYS` | ? | `sk-abc,sk-xyz` | `[]` | Comma-separated Opencode Go API keys. When set and `PRIMARY_PROVIDER=opencode`, routes standard chat/search/inline through `opencode.ai/zen/go/v1` using Bearer auth. Key rotation works identically to Gemini keys. |
| `OPENCODE_AVAILABLE_MODELS` | ? | `opencode-go/minimax-m2.7,opencode-go/qwen3.5-plus` | `[]` | Models shown in the `/model` selector when Opencode is the active provider. If empty, defaults to the 7 canonical Opencode models. Changes made via `/models` admin wizard are stored here. |
| `PRIMARY_PROVIDER` | ? | `opencode` / `gemini` | `opencode` | Runtime LLM provider switch. `opencode` routes through Opencode Go (with Gemini fallback). `gemini` bypasses Opencode entirely. Changed at runtime via `/set_provider` — persisted in `global_settings`, no restart required. |
| `OPENCODE_DEFAULT_MODEL` | ? | `opencode-go/minimax-m2.7` | `opencode-go/minimax-m2.7` | Flagship chat model. |
| `OPENCODE_QNA_MODEL` | ? | `opencode-go/qwen3.5-plus` | `opencode-go/qwen3.5-plus` | Model for quick Q&A search synthesis (`?` prefix). |
| `OPENCODE_RESEARCH_MODEL` | ? | `opencode-go/qwen3.6-plus` | `opencode-go/qwen3.6-plus` | Model for deep research synthesis (`??`). |
| `OPENCODE_VISION_MODEL` | ? | `opencode-go/mimo-v2-omni` | `opencode-go/mimo-v2-omni` | Vision-capable model — supports `image_url` natively. Exempt from Gemini vision redirect. |
| `OPENCODE_INLINE_MODEL` | ? | `opencode-go/minimax-m2.5` | `opencode-go/minimax-m2.5` | Lighter model for inline mode generation. |
| `JINA_API_KEY` | ? | `jina_xxx...` | `""` | API key for [JINA AI Search](https://jina.ai). Used as grounding backend for `?` quick search when Opencode is active. Also used by agentic research engine for page reading via `r.jina.ai`. Without this key, Opencode search queries run without web context. |

> **Canonical Opencode models**: `minimax-m2.7`, `minimax-m2.5`, `qwen3.6-plus`, `kimi-k2.5`, `big-pickle`, `qwen3.5-plus`, `mimo-v2-omni` — all prefixed `opencode-go/`.

---
### ?? OpenRouter Models (Tertiary LLM Provider)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `OPENROUTER_API_KEYS` | ⚙️ | `sk-or-v1-abc,sk-or-v1-xyz` | `[]` | Comma-separated OpenRouter API keys. Required if `USE_OPENROUTER=true`. Rotated same as Gemini keys. Note: OpenRouter is **disabled for multimodal (image) requests** — Gemini is always used for vision. |
| `USE_OPENROUTER` | ⚙️ | `true` / `false` | `false` | If `true`, routes all standard chat, research, and Q&A operations through OpenRouter instead of Gemini. Overrides `DEFAULT_MODEL` etc. with their `OPENROUTER_*` counterparts. Gemini keys are still needed for embeddings and TTS. |
| `OPENROUTER_AVAILABLE_MODELS` | ⚙️ | `stepfun/step-3.5-flash:free,qwen/qwen3-4b:free` | `[]` | Models shown in /model selector when OpenRouter is active. |
| `OPENROUTER_DEFAULT_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | Default model for standard chat on OpenRouter. |
| `OPENROUTER_QNA_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for quick Q&A search synthesis. |
| `OPENROUTER_RESEARCH_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for agentic research synthesis. |
| `OPENROUTER_URL_SELECTION_MODEL` | ⚙️ | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for URL scoring during agentic research. |

---

### 🚀 FreeTheAI (Multimodal Router)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `FREETHEAI_API_KEYS` | ⚙️ | `sk-freetheai-1,sk-freetheai-2` | `[]` | Comma-separated API keys for FreeTheAI. Provides advanced multimodal image generation and Lyria-based audio music models. |
| `FREETHEAI_AVAILABLE_MODELS` | ⚙️ | `vhr/gpt_image_2,or/google/lyria-3-pro-preview` | `[]` | List of models provided by FreeTheAI router. Handled natively to prevent collisions with OpenRouter. |
| `FREETHEAI_DEFAULT_MODEL` | ⚙️ | `cat/claude-4-6-sonnet` | `cat/claude-4-6-sonnet` | Default chat model via FreeTheAI. |

---

### 📔  Search & Web Research

> **Note**: `JINA_API_KEY` is documented in the **Opencode Go Models** section above — it serves dual purpose as both Opencode search grounding and agentic page reader.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TAVILY_API_KEYS` | ✅ | `tvly-key1,tvly-key2` | — | Comma-separated Tavily Search API keys. Used for both the quick `?` search and deep agentic `??` research. Monthly credit budget tracked in the DB with alerting at 97% utilization. |
| `WEATHER_API_KEY` | No | `abc123...` | `""` | API key for WeatherAPI.com. Powers intent-direct weather query handler — weather queries intercepted before reaching the LLM. Without this key they fall through to the LLM. |
| `EXCHANGE_RATE_API_KEY` | No | `abc123...` | `""` | API key for ExchangeRate-API.com. Powers intent-direct currency conversion queries. Without this key currency queries fall through to the LLM. |

---

### 🧠 Agentic Research Engine

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `AGENTIC_MODEL` | ⚙️ | `gemini-2.5-flash` | `""` (uses `RESEARCH_MODEL`) | Overrides the LLM used inside the agentic research loop. Set to a more capable model (e.g. `gemini-2.5-flash`) for better research quality at higher cost. If empty, falls back to `RESEARCH_MODEL`. |
| `AGENTIC_MAX_ITERATIONS` | ⚙️ | `5`—`15` | `5` | Maximum research loop cycles before the agent is forced to synthesize an answer. Each iteration = one round of query → search → read → reflect. Higher = deeper research, higher API cost. |
| `AGENTIC_MAX_PAGES` | ⚙️ | `3`—`10` | `3` | Maximum web pages the agent reads per iteration. Each page consumes Jina/Tavily credits and LLM tokens. |
| `AGENTIC_MAX_TOKENS` | ⚙️ | `100000`—`500000` | `100000` | Hard token budget cap for the entire agentic session. The loop terminates if accumulated prompt + completion tokens exceed this value. Prevents runaway sessions on complex queries. |
| `AGENTIC_TIMEOUT_SECONDS` | ⚙️ | `90`—`300` | `90` | Wall-clock time limit for the entire agentic session. If the loop doesn't finish within this window, a partial result is returned. Increase to `180`+ on powerful VPS for deeper research. |
| `AGENTIC_PAGE_CONTENT_LIMIT` | ⚙️ | `4096`—`16384` | `8192` | Maximum characters extracted from each web page before truncation. Higher = more context per page, more LLM tokens consumed. |
| `ADAPTIVE_THINKING_ENABLED` | ⚙️ | `true` / `false` | `true` | Enables the automatic `thinking_level` selector (14-rule heuristic). When `true`, simple greetings get `low` depth and complex code/research queries get `high`. User's manual `/thinking` setting always overrides this. |

---

### 📊 Rate Limits & Concurrency

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `DAILY_LIMITS` | ⚙️ | JSON: `{"gemini-2.5-flash":250}` or compact: `gemini-2.5-flash:250,gemini-2.5-flash-lite:15` | `15` per model | Per-user daily request caps by model name. Requests beyond the limit receive a "quota exceeded" reply. Tracked in the DB with reset at midnight UTC. The TTS model `gemini-2.5-flash-preview-tts` can be included with a separate limit. |
| `MAX_CONCURRENT_HEAVY_REQUESTS` | ⚙️ | `4`—`32` | `4` | Global asyncio semaphore limiting simultaneously active LLM/TTS requests. Backed by Redis for multi-replica safety. On a single-core free container: `4`. On a 4-vCPU VPS with 8 GB RAM: `16`—`24` is safe (LLM calls are I/O-bound, not CPU-bound). |
| `MAX_CONCURRENT_HEAVY_CALLBACKS` | ? | `4`-`32` | `4` | Separate asyncio semaphore for callback-triggered LLM operations (e.g. inline regeneration, retry buttons). Independent from the main heavy semaphore so callback interactions remain responsive even when the main queue is saturated. |
| `MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS` | ⚙️ | `1`—`8` | `1` | Separate semaphore for agentic research (`??`) sessions. These are memory-intensive due to iterative context accumulation. On a 4-vCPU VPS: `4` is safe. |
| `LRU_STATE_CACHE_SIZE` | ⚙️ | `1000`—`50000` | `1000` | Maximum number of `UserState` objects held in the in-process LRU cache. Each entry is ~2—5 KB. `1000` was conservatively set for free-tier 512 MB containers. On a VPS with 8 GB RAM, set to `20000`+ to dramatically reduce DB round-trips. |
| `DB_POOL_MIN_SIZE` | ⚙️ | `2`—`10` | `2` | See Database section. |
| `DB_POOL_MAX_SIZE` | ⚙️ | `10`—`50` | `10` | See Database section. |

---

### 🎨 Image Generation

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `IMAGE_MODELS` | ⚙️ | `flux,zimage,gptimage,qwen-image,wan-image,klein` | `flux,zimage,gptimage,qwen-image,wan-image,klein` | Pollinations.ai models displayed as buttons in the interactive `/draw` Canvas and exposed in the inline image picker. All 6 models are included by default. Override via env var to restrict the set. |
| `DEFAULT_IMAGE_MODEL` | ⚙️ | `flux` | `flux` | Which Pollinations model is pre-selected by default in the Canvas keyboard. |
| `POLLINATIONS_API_KEY` | ⚙️ | `pollinations-xxx` | — | Optional key for Pollinations.ai. Without it, the API works but with stricter rate limits. Get it at [pollinations.ai](https://pollinations.ai). |
| `IMAGE_GEN_DAILY_LIMIT` | ⚙️ | `10`—`100` | `10` | Per-user daily cap for Imagen 4 generations via Google API. Counted separately from Pollinations. |
| `IMAGE_GEN_RPD_PER_KEY` | ⚙️ | `25` | `25` | Requests-per-day budget per Gemini API key for Imagen 4. The free tier allows 25 RPD. Tracked in Redis with in-memory fallback. Prevents image quota from consuming keys needed for LLM chat. |
| `IMAGE_GEN_TIMEOUT` | ⚙️ | `30.0`—`120.0` | `60.0` | Max seconds to wait for an Imagen 4 API response before timing out and rotating to the next key. |
| `IMAGE_GEN_MAX_RETRIES` | ⚙️ | `1`—`5` | `3` | Number of Gemini key rotation attempts on quota/error before failing image generation entirely. |

---

### 🔊 Voice / TTS (ElevenLabs & Gemini)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `ELEVENLABS_API_KEYS` | ⚙️ | `sk_abc,sk_xyz` | `[]` | Comma-separated ElevenLabs API keys used for outbound voice synthesis. Load-balanced with round-robin rotation. If empty, the system falls back exclusively to Gemini REST TTS (`gemini-2.5-flash-preview-tts`). Free ElevenLabs tier gives ~10k chars/month per key. |
| `ELEVENLABS_VOICE_ID` | ⚙️ | `XB0fDUnXU5powFXDhCwa` | `XB0fDUnXU5powFXDhCwa` | ElevenLabs Voice ID to use for synthesis. Default is Charlotte (conversational, English/Russian). Browse voices at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library). |

---

### 📋 Logging & Observability

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `STRUCTURED_LOGGING` | ⚙️ | `1` / `true` / `json` | Auto-detected | When set, forces JSON-structured log output instead of plain text. Auto-detection uses `HOSTNAME` length and `PORT` presence as heuristics — runs structured in cloud, plain locally. Set `1` explicitly to always force JSON. |
| `LOG_PRETTY` | ⚙️ | `1` / `true` | `false` | Formats JSON logs with indentation for human readability during local development. Disabling in production reduces stdout volume by ~30%. |
| `HOSTNAME` | ⚙️ | Auto-injected by Docker | `unknown` | Docker/Kubernetes injects this automatically. Used as `instance_id` in structured log records for multi-replica tracing. Do not set manually. |
| `SERVICE_NAME` | ⚙️ | `gemaibotv2` | `gemaibotv2` | Service label added to every structured log record. Override if running multiple bot instances under different names. |

---

### 🧪 Testing Only

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TEST_DATABASE_URL` | ⚙️ | `postgresql://user:pass@localhost:5432/test_db` | — | Postgres connection string used **exclusively** by `pytest -m integration`. Must point to a clean, separate database — integration tests run destructive DDL and DML. Never set this to the production DB. |

## Run

**Local Python (dev, long-polling):**

```bash
python bot.py
```

**Docker Compose (legacy local, single container):**

```bash
docker-compose -f docker-compose.yml up -d
```

**Production VPS (3-container stack via GitHub Actions CI/CD):**

The canonical deployment is automated by `.github/workflows/deploy.yml` (triggers on push to `vps_testai` branch). It builds and pushes a Docker image to GHCR, then SSH-deploys 3 containers:

1. **`tg-api`** — Local Telegram Bot API Server (`aiogram/telegram-bot-api:latest`). Requires `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org). On first deploy, the workflow runs `bot.log_out()` against the Telegram cloud to release the token for local API use (one-time, idempotent via `/opt/tg-local-api-migrated` flag file).
2. **`tg-bot`** — The Python bot container. Connected to `tg-api` via `TELEGRAM_LOCAL_SERVER_URL=http://tg-api:8081/bot`. Mounts the shared volume `tg-api-data` at `/var/lib/telegram-bot-api` for zero-copy file access.
3. **`tg-media-cleanup`** — Alpine cron sidecar. Runs `chmod -R g+rX` every 60s (fixes UID 101 permission conflicts) and `find -mtime +7 -delete` every 24h (prevents disk exhaustion from cached media).

All containers share the `tg-net` Docker bridge network and `TZ=Europe/Kyiv`.

If `VERTEX_LIVE_SERVICE_ACCOUNT_JSON` is configured in GitHub Actions secrets, the deploy script also writes that service-account JSON to the VPS, mounts it into `tg-bot` as `/run/secrets/vertex-live-sa.json`, and exports `GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/vertex-live-sa.json`. The mounted file must remain readable by the non-root bot process; the workflow now prepares it with read permissions suitable for the container user instead of root-only mode.

> **Manual deploy (without CI):**
> ```bash
> docker network create tg-net 2>/dev/null || true
> docker volume create tg-api-data 2>/dev/null || true
> 
> # 1. Local API Server
> docker run -d --name tg-api --network tg-net \
>   -e TELEGRAM_API_ID="$TELEGRAM_API_ID" \
>   -e TELEGRAM_API_HASH="$TELEGRAM_API_HASH" \
>   -e TELEGRAM_LOCAL=1 -e TZ=Europe/Kyiv \
>   -v tg-api-data:/var/lib/telegram-bot-api \
>   aiogram/telegram-bot-api:latest
> 
> # 2. Bot
> docker run -d --name tg-bot --network tg-net \
>   -p 127.0.0.1:10000:10000 \
>   -v tg-api-data:/var/lib/telegram-bot-api \
>   -e TELEGRAM_LOCAL_SERVER_URL="http://tg-api:8081/bot" \
>   --env-file .env \
>   ghcr.io/<your-repo>:latest
> 
> # 3. Media Cleanup Cron
> docker run -d --name tg-media-cleanup --network tg-net \
>   -v tg-api-data:/var/lib/telegram-bot-api -e TZ=Europe/Kyiv \
>   alpine:3.21 sh -c 'while true; do chmod -R g+rX /var/lib/telegram-bot-api/ 2>/dev/null; sleep 60; done'
> ```

## Schema Management

All database DDL is managed via **numbered SQL migration files** in `scripts/migrations/`.

| Component | Role |
|---|---|
| `scripts/migrations/000_init_schema.sql` | Complete table definitions (24 tables) — the full bootstrap DDL |
| `scripts/migrations/001-019_*.sql` | Incremental schema changes (ALTER, indexes, RLS, triggers, cleanup) |
| `scripts/migrations/020_add_trgm_hybrid_search.sql` | Enables `pg_trgm` extension + GIN index for hybrid keyword+semantic memory search |
| `scripts/migrations/024_upgrade_gemini_v2_768.sql` | Migrates embeddings to `halfvec(768)` (gemini-embedding-2-preview) |
| `scripts/migrations/025_add_temporal_graph_edges.sql` | Adds `updated_at` + unique constraint to `memory_edges` for temporal upserts |
| `scripts/migrations/026_add_core_persona_edges.sql` | Adds `is_core BOOLEAN` + partial index to `memory_edges` for Core Persona Protection |
| `scripts/migrations/026b_add_predicate_embedding.sql` | Adds `predicate_embedding halfvec(768)` + HNSW index for Semantic Edge Deduplication |
| `scripts/migrations/027_add_edge_provenance.sql` | Adds `source_memory_ids BIGINT[]` + GIN index to `memory_edges` for HippoRAG 2 Dual-Node provenance |
| `scripts/migrations/032_add_wing_room_taxonomy.sql` | Adds `wing`, `room`, `hall_type` to `long_term_memory` and `wing`, `room` to `memory_nodes` with B-tree + partial HNSW indexes |
| `scripts/migrations/033_add_role_diaries.sql` | Adds `role_diaries JSONB DEFAULT '{}'` to `user_state` for MemPalace persistent role diaries |
| `scripts/migrations/034_global_settings.sql` | Creates `global_settings` key-value table for runtime configuration; seeds `inline_thinking_level` default |
| `scripts/migrations/018_add_missing_table_definitions.sql` | Backfill migration for databases that applied `000` without all tables |
| `scripts/migrate.py` | Standalone CLI runner. Runs explicitly during deployment to guarantee schema safety. |
| `app/db/migrations.py` | Internal migration framework. Implements fail-fast behavior: logs a CRITICAL error and aborts on any failure. |
| `app/db/schema.py` | Startup validation — verifies all expected tables exist after migrations |
| `app/db/rls.py` | Row Level Security policy management |
| `app/db/seed.py` | Initial data seeding (admin user, API keys, indexes) |

**Workflow:**
- **Deploy-time:** `scripts/migrate.py` runs in an ephemeral container. If any migration fails, deployment aborts.
- **Startup-time:** `init_db()` -> `create_tables()` -> `setup_row_level_security()` -> `run_migrations()`. If `run_migrations()` detects drift or failure, it alerts admin via Telegram.

**Adding new tables:** Create a new numbered `.sql` file in `scripts/migrations/`, add the table name to `EXPECTED_TABLES` in `app/db/schema.py`, and add RLS configuration to `app/db/rls.py` if needed.

## Long-Term Memory Architecture

Persistent semantic recall stored in the `long_term_memory` table (`pgvector` `halfvec(768)`).

| Parameter | Value | Config Location |
|-----------|-------|-----------------|
| Embedding model | `gemini-embedding-2-preview` (768 dims) | `app/repos/memory_config.py` |
| Max memories per user | 500 | `MAX_MEMORIES_PER_USER` |
| Default TTL | 90 days | `DEFAULT_MEMORY_TTL_DAYS` |
| Min query length (store) | 30 chars | `ai_chat.py` threshold |
| Min query length (recall) | 15 chars | `ai_chat.py` threshold |
| Similarity threshold (floor) | 0.60 (adaptive gap-filter: top − 0.15pp, adaptive_floor min 0.40) | `ai_chat.py` / `memory.py` |
| Recall limit | 5 memories (adaptive thresholding filters noise) | `ai_chat.py` `limit` |
| Query expansion model | `gemini-3.1-flash-lite` (~200ms cheap call) | `QUERY_EXPANSION_MODEL` |
| Consolidation model | `gemini-3.1-flash-lite` | `CONSOLIDATION_MODEL` |

**Storage:** Only user intent is embedded (`user_message[:500]`, `source_type='user_intent'`). Bot replies are discarded to maximize vector density. Saving is asynchronous and non-blocking via `submit_retryable()` with 3 retries.

**Retrieval:** Hybrid Reciprocal Rank Fusion (RRF) combining `pgvector` cosine similarity with `pg_trgm` trigram keyword matching (`k=60` smoothing). Falls back to pure semantic search if `pg_trgm` is not installed. Query embeddings use `task_type='RETRIEVAL_QUERY'`.

**Query Intent Gate:** Before performing LLM-based query expansion, a deterministic heuristic (`_should_expand_query`) evaluates whether the user input is a trivial conversational greeting (e.g., "привет", "спасибо", "ok") or too short (<12 chars) to benefit from keyword enrichment. Trivial inputs bypass the ~200ms Flash-Lite expansion call entirely, saving API quota and reducing latency for ~40% of chat turns. The heuristic is intentionally conservative — when in doubt, expansion runs.

**Multi-Query Expansion:** When the intent gate passes, vague queries like "that framework I mentioned yesterday?" are rewritten by Flash-Lite into keyword-dense search phrases ("Python FastAPI web framework project") before embedding. This dramatically improves recall for ambiguous references.

**Injection:** Retrieved memories are formatted as XML tags and appended to `system_instruction` (Context Engineering pattern):
```xml
<long_term_memory>
  <fact source="2026-03-20">User prefers Python for backend</fact>
  <fact source="2026-03-18">User works at a fintech startup</fact>
</long_term_memory>
```

**Consolidation:** When raw memories exceed ~8,000 tokens OR 7 days since last consolidation, `gemini-3.1-flash-lite` extracts 5—8 atomic persona facts. Raw memories are deleted and replaced with consolidated facts (`source_type='consolidated'`) in a single transaction. Consolidation is gated by a debounce (`should_check_consolidation()`) — checked only every 20th message or every 15 minutes.

### Knowledge Graph Architecture

Relational knowledge is stored as a directed graph in dual tables:

| Table | Purpose |
|-------|--------|
| `memory_nodes` | Entity vertices (name, type, description, `halfvec(768)` embedding) |
| `memory_edges` | Directed relationships (source → predicate → target, weight, `is_core`, `predicate_embedding`, `source_memory_ids`) |

**Graph Extraction:** During memory consolidation, the LLM extracts subject—predicate—object triples from accumulated text. Each triple's subject and object become `memory_nodes`, and the relationship becomes a `memory_edge`. Semantic Entity Resolution (`cosine < 0.12`) merges near-identical entity names at ingestion time to prevent graph fragmentation.

**Semantic Edge Deduplication:** Each predicate is embedded (`predicate_embedding halfvec(768)`). If a new edge's predicate is within cosine distance 0.25 of an existing edge between the same endpoints, the existing edge's weight and `is_core` flag are updated via `ON CONFLICT` upsert instead of creating a duplicate.

**Core Persona Protection:** Edges marked `is_core=TRUE` (name, profession, allergies, permanent identity facts) bypass time-decay entirely in retrieval queries. Once an edge is promoted to core, it stays core (`is_core = memory_edges.is_core OR EXCLUDED.is_core`).

**2-Hop Graph Traversal:** After finding the top-K semantically similar entity nodes, a SQL CTE (`hop1 UNION ALL hop2`) follows outgoing edges from 1-hop neighbours to capture indirect relationships (e.g., asking about "Python" surfaces "FastAPI" via a 2-hop chain). Hop-2 results are labelled `(indirect)` in context.

**Edge Provenance (HippoRAG 2 Dual-Node):** Each `memory_edge` carries a `source_memory_ids BIGINT[]` array linking it back to the original `long_term_memory` rows from which it was extracted. This enables the retriever to surface the full unstructured passage alongside a relevant graph triple, reducing LLM hallucination around short predicates. (Schema ready via migration `027`; retrieval integration is on the Phase 2 roadmap.)

**Scope:** Memory operates globally — all standard chat messages trigger store/recall when `ltm_enabled=True`. Agentic research (`??`) does not store memories but can recall from them.

**User Control:** `/memory` shows paginated viewer with per-item delete. `/clearmemory` wipes all. `/settings` toggles `ltm_enabled`. `/deleteme CONFIRM` deletes all data including memories (GDPR Art. 17).

**Operational Notes:**
- To manually prune: `DELETE FROM long_term_memory WHERE created_at < now() - interval '180 days'`
- The GIN index `idx_ltm_content_trgm` supports the `%` operator for keyword matching; rebuild with `REINDEX INDEX idx_ltm_content_trgm` if needed
- Monitor memory count per user via `get_memory_stats(user_id)` or the admin dashboard

## Scripts

| Command                    | Purpose                                  |
| -------------------------- | ---------------------------------------- |
| `ruff check app/`          | Runs Pyflakes / Style / Bugbear linting. |
| `ruff format --check app/` | Verifies code formatting.                |
| `mypy app/`                | Static type checking for Python types.   |

## Testing

The application features a heavily engineered test suite (**1,845+ unit and integration tests, 100% stable CI-ready**) with **parallel execution** via `pytest-xdist`.

- **Types:** Unit tests (mocked limits/APIs), Integration tests (raw DB connections via `@pytest.mark.integration`), E2E tests.
- **Dependencies:** `pytest`, `pytest-asyncio`, `pytest-xdist`, `pytest-cov`.
- **Prerequisites:** Integration tests require `TEST_DATABASE_URL` (or `DATABASE_URL` in test environments) to a clean Postgres instance.
- **Default behavior:** Running `pytest` automatically uses parallel workers (`-n auto`) via `pytest.ini` defaults and runs **all** tests (unit + integration).

| Test Type            | Command                               | Scope                                |
| -------------------- | ------------------------------------- | ------------------------------------ |
| Unit (default, fast) | `pytest`                              | Pure logic, LLM mock chains, prompts |
| Integration (slow)   | `pytest -m integration`               | Raw PostgreSQL operations, DB states |
| All tests            | `pytest -m ""`                        | Full suite (unit + integration)      |
| Coverage             | `pytest --cov=app --cov-report=term-missing` | Application-wide execution coverage  |

## API / Events / Contracts

**Telegram Commands:**

- **User Commands:**
  - `/start`, `/help` — Initial onboarding & main menus.
  - `/newchat` — Reset context and start a fresh conversation.
  - `/model` — Select the active AI model.
  - `/thinking` — Configure reasoning depth (Auto/Low/Medium/High).
  - `/res` — Toggle Deep Research mode (Tavily-powered).
  - `/settings` — Quick access menu for models, search, and memory toggles.
  - `/stats` — Personal usage metrics, streaks, and API usage stats.
  - `/documents` — Manage and query uploaded PDF/DOCX files.
  - `/roles` — Switch between AI personas/roles. Custom roles support prompt editing (manual replacement or AI-enhanced rewrite with preview and manual tweaking).
  - `/setprompt` — Set a custom system instruction for the current chat.
  - `/save`, `/conversations`, `/switch`, `/rename`, `/delete` — Advanced conversation management (persistence).
  - `/export` — Export the current chat history.
  - `/memory` — Paginated viewer of long-term memories with per-item inline delete.
  - `/clearmemory` — Wipe all long-term vector-indexed memories.
  - `/remind` — Set timed reminders with bilingual time parsing (EN/RU). Supports text, QnA, and agentic AI task delivery.
  - `/draw`, `/img`, `/image`, `/generate` — Imagen 4 text-to-image generation. Interactive Canvas with aspect ratio and model controls after each image.
  - `/games` — Opens the external CC-GH game hub Mini App. Private chats use a native `web_app` button; groups use the direct `https://t.me/b0b_bot/games` link.
  - `/subscribe`, `/unsubscribe` — Manage hourly intelligence brief subscriptions (LTM-topic-aware web research summaries).
  - `/mydata`, `/deleteme` — GDPR compliant data export and account deletion.

- **Admin Commands (Requires `ADMIN_ID`):**
  - `/admin` — Central administration hub.
  - `/listmodels`, `/listusers` — List configured models and registered users.
  - `/adduser`, `/deluser` — Manual user management.
  - `/metrics`, `/rolemetrics` — Detailed system and role-based usage telemetry.
  - `/cachestats`, `/queuestats`, `/docstats`, `/groupstats` — Performance monitoring for different subsystems.
  - `/clearcache`, `/clearoldmetrics`, `/clearolddocs` — System maintenance and cleanup.
  - `/updatetavilykeys`, `/checktavilykeys` — Hot-swap and verify search API keys.
  - `/checkgeminikeys` — Async parallel health check of all Gemini API keys against the Google API.
  - `/set_inline_thinking <level>` — Set inline generation `thinking_level` at runtime (stored in `global_settings` DB table, no restart required). Valid: `minimal`, `low`, `medium`, `high`.
  - `/set_provider <name>` - Switch the primary LLM provider at runtime without restart. Valid: `opencode`, `gemini`, `openrouter`. Stored in `global_settings` and takes effect immediately via in-process cache invalidation. Primary tool for live provider failover.
  - `/models` — Runtime model management wizard. Add, remove, or reset the active Gemini and OpenRouter model lists without container restarts. Changes persist in `global_settings` and sync immediately to all users.
  - `/registergroup` — Authorize the bot for use in a specific Telegram group.
  - `/reloadconfig` — Trigger an immediate hot-reload of the environment configuration.

**Web Dashboard (Quart HTTP Routes):**

- `GET /`, `GET /login`, `POST /login`, `GET /logout` — UI interface (requires `ADMIN_SECRET` authentication and uses Cookie Sessions).
- `GET /health` — Robust unauthenticated API health check.
- `GET /metrics` — Exposes Prometheus telemetry text (uptime, errors, usage).
- `GET /api/dashboard` — Aggregated batch endpoint (replaces 8 individual fetches with 1 RTT). Auth required.
- `GET /api/overview`, `/api/keys`, `/api/errors`, `/api/cache`, `/api/queue`, `/api/database`, `/api/circuit-breakers`, `/api/memory` — Individual JSON data endpoints for dashboard charts (requires auth cookie or `X-Auth-Token` header).
- `GET /api/key-health` — Per-key health diagnostics: status, failure count, suspension info. Auth required.
- `GET /api/events` — Server-Sent Events stream (5s interval) for real-time CPU, memory, DB, queue, and request metrics.

## Main User Flows

- **Inline Mode**
  - _Preconditions_: `/setinline` + `/setinlinefeedback` (100%) configured in BotFather.
  - _Steps_: From any Telegram chat, user types `@gemaibotv2 <query>`. Bot returns 3 tone options + up to 5 image mode buttons (auto-detected via smart routing). User selects tone → placeholder with `🔎 ищет в интернете…` sent to chat. Bot receives `ChosenInlineResult`, runs `_stream_inline_fast()` in background via `TaskManager`: **Vertex AI Express** (`gemini-3.1-flash-lite`) is the primary slot with **Google Search Grounding** (`enable_web_search=True`); 2 AI Studio keys (`gemini-2.5-flash-lite`) race as fallback slots. First valid response wins. Grounding citations from the winner are captured via `_GroundingMeta` sentinel and appended as an expandable `📎 Источники` blockquote (up to 3 URLs). Image prompts auto-route to the best Pollinations model based on intent (quoted text → wan-image, edit verbs → klein).
  - _Expected Outcome_: Full AI answer (with real-time web data + citation sources) appears in the original chat, in-place, within ~5—15 s. On all-rounds failure, user can retry with one tap (`🔄 Повторить`, 5-min TTL).
- **Standard Conversation**
  - _Preconditions_: User selects `/newchat`.
  - _Steps_: User inputs text. The orchestrator embeds it in context, pulls long-term memory via pgvector, and dispatches it to the current AI provider model (Gemini or OpenRouter).
  - _Expected Outcome_: Streaming response appended to the Telegram message.
- **Research Query**
  - _Preconditions_: User triggers Research via `/res`.
  - _Steps_: Bot extracts search intent, retrieves context via Tavily API, scores endpoints utilizing LLM, and synthesizes the finalized context stream.
  - _Expected Outcome_: Sourced and cited comprehensive answer.
- **Admin Dashboard Monitoring**
  - _Preconditions_: Application binds to `PORT`, `ENABLE_WEB_SERVER=true`.
  - _Steps_: Admin visits web URL, completes the Brute-force protected Login Flow using the `ADMIN_SECRET` token.
  - _Expected Outcome_: Live monitoring of metrics, keys usage, memory efficiency, and database connection pools.

## Troubleshooting

- **Conflict Error on Startup**: Usually signifies another bot instance is currently polling the Telegram API using the same Token. Requires closing duplicate instances if not using Webhooks.
- **`decryption_error` traces**: Usually caused by attempting to load the database on a new host without providing the exact prior base64 `ADMIN_SECRET`.
- **Search features hanging**: Check `TAVILY_API_KEYS` exhaustively or verify the `circuit_breaker` state at `/api/circuit-breakers`.

## Known Documentation Gaps

- **Logging Variable Chain**: Legacy compose config sets `LOG_JSON=true`, but runtime uses `LOG_FORMAT` environment variable detected in `app/utils/logging_config.py`. The variable `STRUCTURED_LOGGING` referenced in earlier docs is not present in the `Settings` model — `LOG_FORMAT` is the actual control variable. However, `bot.py:main()` now checks `STRUCTURED_LOGGING` env var directly (alongside `LOG_FORMAT`) for backwards compatibility.
- **OpenRouter Multimodal Capabilities**: OpenRouter is explicitly disabled for multimodal interactions (images); this architectural decision is under-documented in internal application documentation.
- **Healthcheck Start Period**: `Dockerfile` sets `--start-period=40s` while `docker-compose.yml` overrides with `start_period: 120s`. The compose value takes precedence in production.
- **CI pytest.ini Comment**: CI workflow (line 74) comments "runs with -m 'not integration' due to pytest.ini" but `pytest.ini` does not set `-m "not integration"` in `addopts`. All tests (unit + integration) run by default; integration tests simply pass without a database.
- **docker-compose.yml is legacy**: The compose file describes a single-container deployment from the Northflank era. The canonical production deployment is the 3-container stack defined in `.github/workflows/deploy.yml`. The compose file is retained for backwards compatibility but does not include the Local Bot API Server or media cleanup sidecar.

## Architecture Decisions

Key implementation decisions that frequently need re-discovery:

### State Persistence
`UserState` lives in an in-memory `LRUCache` (configurable via `LRU_STATE_CACHE_SIZE`, default 1000). Changes are debounced to PostgreSQL with a 300ms window via `_pending_persists` dict, preventing DB write storms during rapid interactions.

### Error Tagging
Telegram messages carry invisible `ErrorCode` tags via zero-width space characters (`\u200b` + enum value). This enables O(1) error classification from user-visible messages without fragile text/emoji parsing. See `tag_error()` and `classify_from_message()` in `app/errors.py`.

### Streaming Message Overflow
When a streaming AI response exceeds Telegram's ~4000 char limit, `StreamingWriter` finalizes the current message and creates a new one via `reply_new_message()`. The split preserves markdown continuity: `_detect_open_markdown()` closes unclosed code blocks/bold/italic in the frozen message and reopens them in the continuation. All regex patterns used by `_detect_open_markdown()` are compiled once at module level (`_MD_FENCE_RE`, `_MD_STRIP_FENCES_RE`, etc.) — not per-call — to avoid redundant C-level compilation overhead at streaming split boundaries.

### Memory Consolidation Triggers
Consolidation fires when raw memories exceed ~8,000 tokens OR 7 days since last consolidation. A debounce gate (`should_check_consolidation()`) prevents DB queries on every message — it checks only every 20th message or every 15 minutes.

### Singleton Lifecycle
`DatabaseManager`, `ProviderRouter`, and `PromptRegistry` use lazy-init singletons. Tests handle cleanup via `conftest.py` fixtures. Import-time side effects are avoided by deferring initialization to first access. The image-processing worker pool in `app/utils/image_utils.py` also follows this rule as of `v2.12.11`: the `ProcessPoolExecutor` is created only on first real image work, so importing Gemini / multimodal / audio compatibility modules remains safe in restricted test and CI environments.

### Key Rotation Health Scoring
API keys are selected by a two-tier SQL query: first from active keys that haven't exceeded failure thresholds, then from keys whose cooldown period has expired. Per-key health data (failure count, last failure time, suspension reason) is tracked in `repos/keys.py`. The Crocodile judge path participates in the same lifecycle: failed judge requests suspend the offending Gemini key, and successful judge winners explicitly record recovery so transiently unhealthy keys can re-enter the active pool.

## Future / Roadmap

| Feature | Description | Status |
|---------|-------------|--------|
| **Debate Mode** | Multi-model argument synthesis — the bot queries 2—3 models with opposing viewpoints, then synthesizes a balanced answer highlighting agreements, disagreements, and confidence levels. Ideal for complex or controversial topics. | Planned |
| **Shared Group Brain** | Group-level long-term memory — when the bot is added to a Telegram group, it builds a shared LTM across all group members. Group memories are tagged by contributor and searchable by any member. Includes configurable privacy controls (opt-in/opt-out per user). | Planned |

## Testing

The test suite is organized into three levels:

```
tests/
├── conftest.py                   # Global fixtures: DB cleanup, async exception surfacing
├── factories.py                  # Telegram object builders (Update, Message, Context, User)
├── integration/
│   ├── conftest.py               # Transactional DB fixtures (asyncpg rollback per test)
│   └── test_e2e_app_smoke.py    # Integration: real DB, mocked LLM network
├── e2e/
│   ├── test_chat_happy_path.py  # Full pipeline: handle_request → stream → DB persist
│   └── test_stream_recovery.py  # Mid-stream APIError / timeout recovery
├── test_error_codes.py          # ErrorCode tagging, typed/HTTP/string classification
├── test_factories.py            # Factory correctness (structure, independence)
├── test_formatting.py           # TelegramFormatter, escape_format_chars (AAA)
├── test_semaphore_invariants.py # GlobalLLMSemaphore: release on success/exception/cancel
├── test_streaming_writer.py     # StreamingWriter: write/finalize, rate-limit retry, overflow
├── test_text_format_aaa.py      # markdown_to_html, sanitize_html_tags, split_text_safe
└── test_thinking_classifier.py  # classify_thinking_level, resolve_thinking_level
```

**Running tests:**

```bash
# Unit tests only (no DB required)
python -m pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts="

# Full suite including integration (requires TEST_DATABASE_URL)
python -m pytest tests/ --override-ini="addopts="

# Parallel (default from pytest.ini)
python -m pytest tests/
```

**Design principles:**
- All tests follow **Arrange-Act-Assert** strictly — one behaviour per test function.
- Integration tests use **transactional rollbacks** (`asyncpg`) for full isolation.
- External AI/Telegram API calls are replaced by **fake adapters** and **async generator stubs**.
- `GlobalLLMSemaphore` uses the local asyncio fallback path (Redis patched to `None`).

## Contributing

1. Create a descriptive PR.
2. Verify all `pytest` checks pass: `python -m pytest tests/ --override-ini="addopts="`
3. Run `ruff check app/ tests/ --output-format=concise` — zero violations required.
4. Run `mypy app/ --ignore-missing-imports` — exit 0 required.

## License

MIT (Verified via shield badge notation in legacy files).
