# GemAI Bot v2

An advanced, asynchronous Telegram bot designed as a comprehensive AI assistant. It orchestrates multiple AI providers (Google Gemini, Opencode Go, OpenRouter), performs web research via native Google Search Grounding, JINA AI Search, and Tavily, maintains long-term contextual memory, and processes multimodal inputs (voice, images, documents).

## What It Does

The bot provides intelligent conversational abilities within Telegram, augmenting standard LLM replies with real-time internet search capabilities and multimodal processing (voice transcription, image analysis, PDF/DOCX). It actively manages AI API quotas using a key rotation system with automatic suspension on **503/UNAVAILABLE and 429/RESOURCE_EXHAUSTED** errors â” including inside the Crocodile game judge race (quota keys are suspended until midnight PT, transient keys for 15 s, permanent keys indefinitely). Stores chat histories and extracted semantic concepts in a PostgreSQL database, and exposes an administrative health dashboard.

## Current Status

**Production-Ready** (`v2.14.0`). Deployed as a **3-container Docker stack** on a DigitalOcean VPS: Local Telegram Bot API Server (MTProto), Python bot (Quart + Hypercorn + PTB v20+ webhook), and an Alpine-based media cleanup cron. Uses `concurrent_updates(50)` with a Quart webhook handler to decouple update processing from HTTP acknowledgment. Built-in telemetry, circuit breakers, and connection pooling. The testing architecture follows strictly deterministic Arrange-Act-Assert (AAA), achieving a CI-ready 100% pass rate across over 1813 unit and E2E integration tests.

## Features

- **Smart Provider Routing (Split-Brain Architecture)**: Three-tier provider system with automatic failover. The **primary tier** is **Opencode Go** (models: `minimax-m2.7`, `minimax-m2.5`, `qwen3.6-plus`, `kimi-k2.5`, `big-pickle`, `qwen3.5-plus`, `mimo-v2-omni`) — a performant LLM inference cluster at `opencode.ai/zen/go/v1` (Bearer auth). When Opencode is exhausted or fails, the system **automatically cross-falls back to Google Gemini** (`gemini-3.1-flash-lite-preview`, `gemini-3-flash-preview`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`). **OpenRouter** is available as a tertiary option. Provider is runtime-switchable via `/set_provider` (admin, no restart needed). Gemini keys always required for embeddings, TTS, and image generation. Features **Race Requests** (two API keys race in parallel — first chunk wins, loser cancelled), **Model Cascade Fallback** (auto-downgrade on sustained 503 errors), and **Redis-backed Deferred Queue** (background retry after total outage).
  - **Opencode → Gemini Cross-Provider Fallback**: If all Opencode keys are exhausted, the router automatically delegates to the Gemini fallback chain without user-visible interruption. The `_is_fallback=True` flag prevents infinite recursion. The fallback map is built dynamically at call time (`_get_opencode_gemini_fallback()`) so hot-reloaded `DEFAULT_MODEL`/`RESEARCH_MODEL`/`QNA_MODEL` values are reflected immediately — no stale state.
  - **JINA Search Grounding**: For Opencode-routed `?` quick search, uses **JINA AI Search** (`s.jina.ai`) instead of native Gemini grounding. Returns LLM-ready markdown injected as `<search_context>` XML in the system prompt. Configured via `JINA_API_KEY`.
- **Quick Search (`?` prefix)**: Single-call web search with dual grounding paths. For **Gemini** models: uses **native Google Search Grounding** (LLM queries the web internally, no extra network hops). For **Opencode** models: uses **JINA AI Search** (`s.jina.ai`) returning LLM-ready markdown injected as `<search_context>` system prompt blocks. Uses a resilient model fallback chain for low latency.
- **Inline Mode (Cross-Chat Bot Interaction)**: Users invoke the bot from **any Telegram chat** by typing `@gemaibotv2 <query>`. Features an **Advanced Progressive UX**:
  - **Implicit Media Swap**: Prompts starting with `"Ð½Ð°ÑÐ¸Ñ ÑÐ¹"` immediately push a fast Pollinations.ai image to the inline grid (via static placeholder from `placehold.co`), which is then asynchronously generated and swapped in-place via a **two-step admin-chat file_id minting** pattern: the raw bytes are sent to the admin chat via `bot.send_photo` to obtain a stable `file_id`, the temp message is deleted immediately, and the minted `file_id` is passed to `edit_message_media` â” the only reliable method accepted by the Telegram Bot API for inline message media edits in `local_mode=True`.
  - **Tabbed Response UI**: Inline responses are structured using XML tags (`<tldr>`, `<details>`, `<sources>`) extracted by the LLM and rendered dynamically via inline buttons. Users can seamlessly switch tabs without re-triggering generation. Admin-toggleable via `/set_inline_tabs <on|off>`.
  - **Collaborative AI-Notes**: Prefixing a query with `Ð´Ð¾Ñ ÐºÐ°: <topic>` initializes a persistent, shared workspace. Any user can reply to the board to add notes (bypassing privacy mode via `via_bot` detection). The bot debounces new entries (60s window) and automatically synthesizes them into an evolving structural summary via the `TaskManager`. Backed by PostgreSQL `inline_boards`.
  - **Core Architecture**: Powered by `gemini-3.1-flash-lite-preview` with **dynamic `thinking_level`** and real-time context via **native Google Search Grounding** (`enable_web_search=True`). Uses a dedicated **`_stream_inline_fast()` 3-way Race Requests** accumulator: 3 API keys fire simultaneously per round up to 12 slots. Grounding citations are surfaced as an expandable `ð“ ÐÑ ÑÐ¾ÑÐ½Ð¸ÐºÐ¸` blockquote (up to 3 URLs) at the end of the response. **5-Mode Smart Image Routing**: inline queries automatically detect intent â” quoted text (`Â«Â»""`) â’ `wan-image` (ÐÐµÐ¼/Ð¢ÐµÐºÑ Ñ), edit verbs â’ `klein` (ÐÐ·Ð¼ÐµÐ½Ð¸ÑÑ ÑÐ¾ÑÐ¾), or manually selectable: `zimage` (Ð¢ÑÑÐ±Ð¾), `gptimage` (Ð£Ð¼Ð½ÑÐ¹), `qwen-image` (Ð ÑÑ). Background generation tasks are managed by the centralized `TaskManager` (graceful shutdown drain, MAX_TASKS=100 cap). On failure, translation loops gracefully trap errors and attach a **ð” ÐÐ¾Ð²ÑÐ¾ÑÐ¸ÑÑ** inline retry button for one-tap re-generation. Requires `/setinline` + `/setinlinefeedback` at 100% in BotFather.
- **Agentic Web Browsing (`??` prefix)**: Deep research mode utilizing Tavily API and Jina Reader API for multi-step query decomposition, autonomous site triage, content extraction, and dynamic self-correction loops. Hardened against memory leaks caused by gRPC protobuf cyclic references during long-running iterations (including threaded, non-blocking asynchronous Garbage Collection). Per-call API key usage tracking ensures accurate quota accounting across all LLM invocations within the agentic loop. Features an intelligent **Model Fallback Cascade** (automatically retries failed LLM requests or 503 errors using the next most capable model according to the capability tier rankings), parallel tool execution (`asyncio.gather` with semaphore), two-layer page content caching (session + global, 30-min TTL), source quality scoring (domain classification, freshness labels, citation validation), adaptive iteration budget (query deduplication, configurable token cap and wall-clock timeout), and rich streaming progress with search queries and iteration counters.
- **Image Processing Pipeline**: Context-aware adaptive resize (`TASK_DIMS`: describe 1280px, search 768px, OCR 2048px) governed by **Shannon Entropy Analysis** (dynamically boosts +50% dimension for text-dense screenshots while reducing -25% for simple photos, optimizing token usage). Uses a 3-stage compression pipeline (thumbnail â’ JPEG q85 â’ fallback q75/65), TTL-cached results (`cache_key` by `file_unique_id`), and `TaggedImage` metadata carrier across handlerâ’provider boundary to eliminate redundant recompression. Media group downloads use `Semaphore(5)` with debounced progress indicator.
- **Image Generation (Dual Provider)**: Text-to-image generation via `/draw <prompt>` or via **implicit natural language triggers** (e.g., *"Ð‘Ð¾Ñ, Ð½Ð°ÑÐ¸Ñ ÑÐ¹ ÐºÐ¾ÑÐ°"* / *"Ñ Ð³ÐµÐ½ÐµÑÐ¸ÑÑÐ¹ ÐºÐ°ÑÑÐ¸Ð½ÐºÑ Ð»ÐµÑ Ð°"*). Uses a multi-layered Regex heuristics engine to isolate the artistic prompt without leaking pronouns or conversational fillers (e.g. extracts "Ð»ÐµÑ Ð°" from "Ñ Ð³ÐµÐ½ÐµÑÐ¸ÑÑÐ¹ Ð¼Ð½Ðµ Ð¿Ð¾Ð¶Ð°Ð»ÑÐ¹Ñ ÑÐ° ÐºÐ°ÑÑÐ¸Ð½ÐºÑ Ð»ÐµÑ Ð°"). Implicit triggers are natively intercepted in both text and voice channels. Voice requests trigger an **Interactive Pre-Canvas Confirmation** where the parsed text and generation keyboard are rendered interactively before consuming API resources. Uses a Factory Pattern for provider routing:
  - **Google Imagen 4** (`imagen-4.0-fast-generate-001`, etc.): Triggered when the user requests an `imagen-*` model and `GEMINI_API_KEYS` are available. Features an **isolated per-key RPD budget** to protect chat quota.
  - **Pollinations.ai** (Models: `â¨ Flux`, `â¡ Z-Image`, etc.): The primary provider for free-tier keys, capable of operating completely without an API key. Uses robust transport layer fallback: attempts OpenAI-compatible `POST` for structured errors, failing over to a direct `GET` stream with `Content-Type: image/*` validation if the primary endpoint timeouts or 5xxs.
  - **Interactive Canvas UX**: Features full, unrestricted prompt display (up to 800 characters) across the entire UI. Heartbeat animation (`ChatAction.UPLOAD_PHOTO` refreshed every 4.5 s) during generation, followed by an inline keyboard for one-tap regeneration, aspect ratio switching (1:1, 3:4, 4:3, 9:16, 16:9), and dynamic model switching. Includes a native "â ï¸  ÐÐ·Ð¼ÐµÐ½Ð¸ÑÑ Ð¿ÑÐ¾Ð¼Ð¿Ñ" pasteboard workflow for frictionless prompt editing. The model selection buttons are auto-generated from environment variables (`IMAGE_MODELS`) with smart column-balancing.
- **Document Understanding**: Extracts text from PDF/DOCX files and uses it for context-aware Q&A.
- **Multimodal Processing Pipeline**: Voice messages transcribed via `gemini-3.1-flash-lite` (high thinking budget for ASR quality) with intent-aware routing (`INTENT:CONVERSATIONAL`, `INTENT:TRANSCRIPTION`, `INTENT:SEARCH`). Features **Smart Voice Auto-Routing** (bypasses manual confirmation UI for low-complexity transcripts) with regex-based fluff tolerance, **Voice-to-Search Auto-Routing** (when `INTENT:SEARCH` is detected, directly invokes **WeatherAPI.com** for weather (1 request, localized RU conditions + "feels like"), **ExchangeRate-API** for fiat currency (RUB/KZT/UAH support), and **CoinGecko** for crypto (BTC/ETH/SOL/TON with Russian aliases) â” zero LLM cost â” then falls back to **QnA Grounded Search** via `gemini-2.5-flash-lite` with native Google Search Grounding for general factual queries; for users in deep-dive or search-enabled mode, routes to the full **Agentic Research Pipeline** instead), **QnA History Persistence** (QnA voice search results are saved to `chat_state.history` and persisted via `update_user_chat`, matching the deep research path â” previously these turns were silently dropped), and **Show & Tell** (voice-replies to photos dynamically inject the image into the LLM context). The **Voice Engine 4.1** pipeline powers outbound voice replies using **ElevenLabs TTS** as the primary provider with atomic fallback to Gemini REST TTS (`gemini-2.5-flash-preview-tts`). Audio is transcoded into Telegram-compliant PCMâ’OGG Opus via `ffmpeg` at **24k bitrate** (optimized for speech). The Gemini TTS engine uses **Adaptive Sequential Chunking** (1800 max bytes) to protect the model from structural memory limits, and features an **Asynchronous Race Requests** architecture: firing 2 TTS keys simultaneously, with the first chunk winning and the loser task efficiently drained via a **Zero-Delay Exception Sink** (bypassing blocking timeout loops for instant audio delivery). A highly optimized Steerable Voice prompt enforces **Strict Verbatim Constraints** and features **Dynamic Personalities** tied directly to the MiniApp's **Independent TTS Temperature** slider (shifting between strict news-anchor, conversational, or highly engaging storytelling). Finished with a low-threshold PCM silence trimming gate (400 amplitude). Featuring **Zero-Latency Voice Intent Detection** (`[VOICE]` tag stripping). Users can hit the **Re-transcribe (Flash)** button for stubborn transcriptions. Media types stored as long-term memories via `submit_retryable()` tasks.
- **Resilient Streaming & Mid-Stream Error Recovery**: Gracefully handles API failures (like 503 Service Unavailable) that occur *during* active streaming. The payload stops, the chunking layer intercepts the error, and a localized footer (`â ï¸  Ð¾ÑÐ²ÐµÑ Ð±ÑÐ» Ð¿ÑÐµÑÐ²Ð°Ð½ Ð¸Ð·-Ð·Ð° Ð¾ÑÐ¸Ð±ÐºÐ¸ Ñ ÐµÑÐ²ÐµÑÐ°`) is appended. Users are immediately shown `[â–¶ï¸  ÐÑÐ¾Ð´Ð¾Ð»Ð¶Ð¸ÑÑ]` and `[ð” Ð—Ð°Ð½Ð¾Ð²Ð¾]` recovery buttons to seamlessly inject partial output back into the conversation context, allowing the LLM to resume generation without context loss. Features a stabilized streaming state machine that eliminates legacy UI placeholder overrides (Phantom Draft Mode cleanup). **Delayed UX Feedback**: if no chunks arrive within 5 seconds, a transparent status toast (`â ³ Ð—Ð°Ð¿ÑÐ¾Ñ  Ð² Ð¾Ð±ÑÐ°Ð±Ð¾ÑÐºÐµ...`) with a `[â  ÐÑÐ¼ÐµÐ½Ð¸ÑÑ]` inline button replaces silent waiting. **TTFB-based Stall Tracking**: detects genuinely stalled connections (>15s waiting for HTTP headers) and allows new user messages to cancel only stale tasks while preserving healthy active streams. **Gemini Search Hallucination Filter** (v2.12.11): `StreamingWriter.write()` now strips only explicit leaked internal `[tool_code] ... google_search.search(...)` traces while preserving legitimate fenced code samples and explanatory references.
- **Internationalization (i18n)**: Content-based language detection (Cyrillic density heuristic) with full bilingual UI (Russian/English). All user-facing strings externalized to `app/i18n.py` registry with `t(key, lang, **kwargs)` lookup. Language detected from message content, not Telegram settings.
- **Persistent GraphRAG Memory**: Semantic recall via `pgvector` (`halfvec(768)`) with **Adaptive Thresholding RRF** retrieval (cosine similarity + `pg_trgm`, over-fetch Ã—2 then gap-filter â¤15pp from top score). **LLM-as-Judge Fallback**: when primary search (floor ~0.48) returns nothing, a second pass at floor 0.42 feeds low-confidence candidates to Flash-Lite, which judges each for genuine relevance â” a "recollection path" inspired by RF-Mem (2025) dual-process memory. **2-Hop Knowledge Graph Traversal** (SQL CTE `hop1 âª hop2`) surfaces indirect relationships, e.g. FastAPI when asking about Python. **Multi-Query Expansion** rewrites vague queries (Flash-Lite LLM, ~200ms) into keyword-dense search phrases before embedding. **Semantic Edge Deduplication** (cosine < 0.25) merges near-identical predicates at consolidation time. **Core Persona Protection**: edges marked `is_core=TRUE` (name, profession, allergies) bypass time-decay and always rank first in graph context. Features **Semantic Entity Resolution** (`< 0.12` distance merging) to prevent graph fragmentation, and **Temporal Edge Upserts** (`ON CONFLICT` updates). Voice and media memories are **Enriched with Modality/Tone Tags** (e.g., `[VOICE, Tone: X]`) via dedicated system prompts. System clusters relational knowledge into dual tables (`memory_nodes`, `memory_edges`) for entity graphing. Memories injected into `system_instruction` as structured `<memory_palace>` XML tags (Context Engineering). Only user intent is embedded for maximum vector density (`source_type='user_intent'`). Dynamic consolidation triggers at ~8,000 tokens or 7 days, extracting atomic persona facts and relationships via LLM. User-manageable via `/memory` (paginated inline UI with per-item delete) and toggleable via `/settings`.
  - **MemPalace Wing/Room Taxonomy**: Every memory and graph entity is classified into a 5-wing hierarchy (`identity`, `projects`, `social`, `knowledge`, `temporal`) with 4â“5 rooms each and 6 hall types (`fact`, `opinion`, `event`, `plan`, `preference`, `habit`). Classification via LLM (admin-configurable model via `TAXONOMY_MODEL`). Partial HNSW indexes on high-traffic wings for sub-10ms targeted retrieval.
  - **AAAK Tiered Context Compression**: 4-layer memory stack inspired by AAAK lossless shorthand â” L0 core facts (JSON shorthand, ~250 tokens), L1 active context (consolidated memories + role diary, ~600 tokens), L2 semantic recall (graph-augmented search, ~1500 tokens), L3 full history (existing assembler). Replaces monolithic memory injection with structured `<memory_palace>` XML blocks.
  - **2-Stage Contradiction Detection**: Embedding distance triage (<0.15 = merge, 0.15â“0.35 = LLM-judge, â¥0.35 = temporal close) with Flash-Lite judge verdicts: `update`, `parallel`, or `refinement`.
  - **Persistent Role Diaries**: Each custom role accumulates session insights (key learnings, preferences, style observations) as JSONB entries, automatically injected as L1 context for cross-session continuity.
  - **Real-Time Streaming Extraction**: Every qualifying user message (â¥30 chars) fires a background `asyncio.Task` that runs Gemini Structured Outputs (Pydantic schema + `thinking_level="medium"`) to immediately populate the knowledge graph â” no more waiting for the 7-day consolidation cycle.
  - **Temporal Conflict Management**: Memory edges have `valid_from`/`valid_to` lifecycle columns. When a user's facts change (e.g., job switch), old edges are closed (`valid_to = now()`) and new ones inserted, preserving complete history. The LLM receives bilingual `<temporal_context>` blocks to celebrate life changes naturally.
  - **RLHF Feedback Cascading**: When a user taps ð‘ on a response, the specific graph edges used are penalized, and a negative feedback count cascades to the original `long_term_memory` records. The retrieval engine reads `rlhf_negative_count` and applies a search-time similarity penalty (-0.03 per vote), effectively burying incorrect facts while surfacing better memories. The bot pre-places ð‘ /ð‘ reactions on its own messages as a silent invitation for feedback.
  - **Agentic RAG**: The agentic research loop (`??` prefix) gains a `recall_memory` tool declaration that lets the LLM proactively query the user's personal knowledge graph during research, grounding web answers in user-specific context.
  - **Multimodal Memory**: Image and audio messages trigger background graph extraction after transcription/description, with `file_id`/`file_type` stored on memory nodes for future media re-delivery.
  - **Group Chat Social Graph**: Group messages fire social graph extraction attributed to the specific speaker (`actor_user_id`) within group context (`chat_id`). Privacy isolation via `is_public` flag and RLS-scoped queries.
  - **Knowledge Graph Visualization API**: Mini App endpoint (`/webapp/api/graph`) serves nodes and edges as JSON for interactive visualization with optional query-based filtering.
- **Distributed Concurrency**: Multi-tier Redis-backed global semaphores (heavy and ultra-heavy limits) to prevent API quota starvation in multi-replica deployments while guaranteeing isolation between standard queries and intensive Agentic research loops.
- **Resilient Operations**: Instance-based background task manager with exponential backoff, bare-coroutine safety guard, and admin alerting hooks. Atomic metrics persistence with delta-based increments prevents data loss on restart. Prompt registry validates required variables at render time to prevent silent placeholder leaks.
- **Thinking Level Control**: Configurable reasoning depth for supported models.
- **Adaptive Thinking Budget**: Automatic `thinking_level` selection via 14 regex heuristics + context-aware escalation. Simple greetings get `low`, code/math/multi-step queries get `high`. User explicit preference always overrides.
- **Conversation Branching**: Fork current chat into a "what-if" branch via snapshot. Explore alternative conversation paths without losing the main thread. One-click restore to the original context.
- **Smart Context Window**: Model-specific token budgets (flash-lite: 32K, flash: 128K â” evidence-based on context degradation research) with automatic context trimming and LLM-backed summarization of dropped history.
- **Agentic Smart Reminders**: DB-persisted user reminders (`/remind 30m Check logs`) with 60s poll-based delivery via `job_queue`. Supports **Zero-Latency Intent Classification** (automatically detects whether a prompt requires a simple text notification, quick QnA search, or deep agentic research). AI tasks run in non-blocking background tasks (`asyncio.create_task`) with concurrency semaphores (max 3), 5-minute timeout guards, and inline â  cancel buttons in the reminder list.
- **Context Summarization**: Automatic token compression for large chats via `app/context/` subsystem â” `ContextAssembler` orchestrates history assembly within model-specific token budgets, `Summarizer` produces LLM-backed compressed summaries, and `TokenBudget` maps model patterns to limits (flash-lite: 32K, flash: 128K).
- **Document Chunking**: Retrieval-time chunking (`app/documents/chunking.py`) with three strategies â” recursive (paragraph/sentence/word), hierarchical (parent/child), and query-aware relevance scoring (`chunk_for_context`) â” replacing naÃ¯ve hard-truncation.
- **Intelligence Briefs**: DB-persisted topic subscriptions (`/subscribe`, `/unsubscribe`). Hourly job extracts topics from LTM â’ Tavily search â’ Gemini summary â’ Telegram delivery. Backed by `brief_subscriptions` table with RLS.
- **Telegram Mini App**: Native in-app settings panel served as a Quart Blueprint (`/webapp/`). Three-tab interface: **âï¸  Settings Editor** (system prompt, model, thinking level, LTM/search toggles with adaptive density for desktop precision), **ð¸ï¸  Knowledge Graph** (interactive force-directed graph canvas with drag, mouse-wheel pivot zoom, and top-left quick-access nav), and **ð§  LTM Explorer** (paginated memory browser with swipe-to-delete and desktop hover-reveal trash button, search, usage stats). Full cross-platform UX: **floating glassmorphic dock** tab-bar on desktop, wheel-redirected horizontal chip scroll with animated arrows and gradient masks, adaptive context-reset button (double-click on pointer devices, hold-to-confirm on touch), `overscroll-behavior` isolation to prevent Telegram close gestures, and `keepalive` fetch for guaranteed memory deletion even when the app is closed mid-undo-window. Tab-bar navigation with haptic feedback. Authenticated via Telegram `initData` HMAC-SHA256. Styled with Telegram theme variables for automatic dark/light mode. Accessible via `WebAppInfo` button in `/settings` command. Uses `WEBHOOK_URL` env var for multi-deployment portability.
- **Crocodile Mini App Game**: 1-on-1 charades game launched from inline mode (`@bot ÐÑÐ¾ÐºÐ¾Ð´Ð¸Ð»`). Player A is shown the hidden word; Player B guesses via a Telegram Mini App WebSocket session. **How to start:** `@bot ÐÑÐ¾ÐºÐ¾Ð´Ð¸Ð»` (random word), `@bot ÐÑÐ¾ÐºÐ¾Ð´Ð¸Ð»:Ð–Ð¸Ð²Ð¾ÑÐ½ÑÐµ` (category), `@bot ÐÑÐ¾ÐºÐ¾Ð´Ð¸Ð»:=custom` (custom word, known only to creator A). A **4-tier semantic judge** evaluates each guess: Levenshtein exact match â’ 24h Redis judgement cache â’ RaceÃ—3 LLM (`gemini-3.1-pro` + `gemini-2.5-flash-lite` fallback) â’ hardcoded no-match fallback. "Midnight Glass" glassmorphism UI features animated feedback cards (ð§/ð¤ /ð”¥/ð¯) and attempt progress bar.
  - **Spectator Mode (God Mode):** Creators who start games using a custom word are placed into an interactive Spectator Mode. They cannot guess, but they receive a real-time synchronized feed of the guesser's chat bubbles. The UI features a fixed Target Word Banner, live typing indicators, and a persistent Reaction Bar allowing the creator to send live emojis (`ð”¥`, `â ï¸ `, `ð`, `ð‘ `, `ð¤”`) that fade into the guesser's chat stream. Driven by an in-memory PubSub system (`asyncio.Queue` based) decoupling events from socket loops.
  - **Instant Game Start & Async Generators (Bug 6.3/6.4):** Custom words (=крокодил) instantly assign the static category Слово игрока (особое), eliminating up to 15s of blocking LLM category classification delays. Randomly chosen games eliminate initial starvation delays by generating a single word fast (via OPENCODE_INLINE_MODEL with a strict 7s timeout), delegating the full 20-word bank fill to a background syncio.create_task worker.
  - **Security hardened (v2.12.8):** WebSocket connections require valid Telegram `initData` HMAC-SHA256 â” unauthenticated connections receive `4003 initData required` immediately. Creator Guard protection restricts guessing actions natively, decoupling authorization boundaries. API key material is sanitized from frame locals before race coroutines are spawned. `_game_locks` dict is bounded to 512 entries with LRU sweep to prevent memory growth from abandoned connections. Backed by **130+ automated tests** via `pytest-xdist`, mocking 100% of LLM calls, Quart endpoints, and WS pipelines. Requires no new env vars â” uses existing `REDIS_URL`, `WEBAPP_BASE_URL`, and `GEMINI_API_KEYS`.
  - **Judge Tolerance (v2.12.9):** `GuessJudgement.hint` `max_length` raised from 80 â’ 255 characters. Resolves a `Pydantic ValidationError` cascade triggered when the primary model (`gemini-3.1-flash-lite-preview`) returns 503 and the fallback (`gemini-2.5-flash-lite`) generates verbose hints exceeding the old limit.
  - **Emoji Temperature Prefix (v2.14.0):** Every judge response now carries an automatic emoji prefix based on semantic score (`🧊`/<30%, `🟡`/<70%, `🔥`/<92%, `🎉`/≥92%). The AI’s witty hint text is preserved 100%—the temperature is prepended on the backend before the WebSocket event fires.
  - **Inline Message Thermometer (v2.14.0):** After every new best score the bot silently `edit_message_text`s the inline message showing `🔥 Лучшая попытка: [██████░░░░] 60%`. Persisted via `best_score` field in Redis.
  - **Graceful Surrender (v2.14.0):** The guesser can tap a `Сдаться` button. The game ends cleanly with `🏳️ Игрок сдался.` and both players receive a `surrendered` WebSocket event.
  - **Creator “God Mode” Custom Hints (v2.14.0):** A creator can type any hint text in their WebApp — it is relayed instantly to the guesser via the Pub/Sub bus. Zero LLM calls.
  - **12 Word Categories (v2.14.0):** Added `Транспорт`, `Одежда`, `Музыка`, `Космос` (15 words each, bilingual RU+EN) plus 17 new aliases. Word-category LRU cache (`category_cache.json`, 10k entries) means any previously-seen custom word is classified in <1ms.
  - **Local Heuristics Hardening (v2.14.0):** `_homogenize_pair` now maps `ё→е` and strips trailing punctuation before Damerau-Levenshtein matching. A guess of `кот.` or `бобёр` is accepted locally without an LLM call.
- **Smart UX Interactions**: RLHF feedback via a two-stage "ð“  ÐÑÐµÐ½Ð¸ÑÑ" inline toggle that expands into ð‘ /ð‘ choices to reduce UI clutter. Citation badge `[ð“ N ÑÐ°ÐºÑÐ¾Ð² (interactive)]` shows when memory was used â” tapping it displays an alert with the exact graph relationships and sources used to generate the response. Smart LLM-generated suggestions (`[SUGGESTIONS:...]` tags â’ inline buttons via memory-cached hash identifiers to bypass Telegram's data limits), intent routing (`[INTENT:...]` â’ contextual actions), `CopyTextButton` for code blocks, `sendMessageDraft` for input pre-filling, and `ð”¥` message effects for image generation.
- **Native Mini App Reader (SSR Architecture)**: High-performance delivery for long responses (>4000 chars). Content is instantly saved to Redis and rendered inside a **Telegram Mini App**. The reader now uses **Server-Side Rendering (SSR)**: Markdownâ’HTML conversion, TOC extraction, and Bionic Reading transforms are performed on the server (`app/utils/reader_utils.py`) before the page is sent â” achieving instant FCP with no skeleton loading state. **Cold-Storage Reverse-Proxy**: When the 24h Redis TTL expires, the reader transparently fetches and parses the associated Telegraph page, serving it through our own UI instead of redirecting. Graceful fallback chain: Redis hit â’ Telegraph proxy â’ Telegraph link redirect. **UX features**: floating TOC FAB (bottom sheet, swipe-to-close, haptic feedback), full-screen code modal with syntax re-highlighting, 20+ language file download, Bionic Reading toggle (word-stem bolding, `sessionStorage` persistence), and browser TTS ("ð” Ð’Ñ Ð»ÑÑ…" / "â ¹ Ð¡ÑÐ¾Ð¿"). Code blocks include a one-click copy button (â“ confirmation), gradient overflow indicators, and file download support.
- **Auto TTS for Research**: Fire-and-forget voice synthesis of research results via ElevenLabs, triggered automatically after successful agentic search responses.
- **Administrative Dashboard**: Quart-based web server serving Prometheus metrics (`/metrics`), system health overviews, batch API (`/api/dashboard` â” 8 metrics in 1 RTT), SSE live updates (`/api/events` â” 5s real-time stream), and key health diagnostics (`/api/key-health`). Frontend integrates SSE EventSource for real-time CPU/memory/queue updates between polls.
- **Request Deduplication & Debouncing**: In-memory double-tap prevention middleware with 3s window and MD5 hashing blocks duplicate identical requests. Rapid-fire text messages and grouped forwards are handled by a **1.1s Trailing Message Debounce** aggregation window. The timer resets on every incoming fragment, flawlessly merging long bursts of messages into a single cohesive AI context before processing.
- **Key Rotation Observability**: Structured `KEY_EVENT` logging for usage milestones, near-limit warnings (70%), threshold rotations, and a `get_health_summary()` dashboard API with per-key status snapshots. **Game Judge Key Rotation (v2.12.10):** the Crocodile judge `_one_call` now classifies each API exception via `classify_key_error()` and fires `KeyHealthRepository.suspend_key()` as a background task â” `quota` errors (429 RESOURCE_EXHAUSTED) suspend the key until midnight PT, `transient` errors (503) for 15 s, `permanent` errors indefinitely. `resolve_ai_request()` already filters suspended keys at SQL level, so the next race round automatically receives a fresh key. All judge call attempts are recorded via `record_api_call("gemini_judge")` and overall latency via `record_request("judge", elapsed, success)`. Full `record_api_call` / `record_request` instrumentation also added for `gemini_chat`, `gemini_transcribe`, and `gemini_vision` pipelines (v2.12.10).
- **Dynamic Key Management**: Centralized `/keys` admin wizard providing an inline keyboard UI to securely view, edit, and clear API provider keys (Weather, Exchange, Gemini, etc.) at runtime without restarting the application. Includes automatic 30-min background health checks pointing to provider verification endpoints, alerting the admin via Telegram if a provider fails.
- **Structured Error Classification**: O(1) type-based error classification via `ErrorCode` enum (17 exception types + 8 HTTP status codes), replacing fragile emoji/text pattern matching. Full error-to-user-message mapping.
- **Graceful Shutdown**: Two-phase drain (pending state persists + task queue) before resource cleanup, preventing data loss during deploys. Includes shutdown hooks for `intent_router` HTTP clients and background task managers.
- **Intent Direct Routing**: Lightweight API bypass for simple utility queries â” intercepted before hitting the LLM, providing near-instant responses even during complete API outages. **Weather** via WeatherAPI.com (single request: geocoding + forecast + localized Russian conditions + "feels like" temperature; graceful fallback to Open-Meteo when API key is absent). **Fiat currency** via ExchangeRate-API v6 (supports RUB/KZT/UAH/KGS/UZS; 1,500 req/month free tier; fallback to Frankfurter for EU pairs). **Crypto** via CoinGecko Demo API (keyless, 30 rpm; BTC/ETH/SOL/TON with Russian aliases like "Ð±Ð¸ÑÐºÐ¾Ð¸Ð½", "Ñ ÑÐ¸Ñ", "ÑÐ¾Ð½"; shows USD + RUB price and 24h change). Multi-day/temporal weather queries ("Ð·Ð°Ð²ÑÑÐ°", "Ð²ÐµÑÐµÑÐ¾Ð¼") are routed to the LLM with Google Search Grounding instead of raw API calls. Russian locative/prepositional case suffixes are stripped before geocoding ("Ð¡Ð°ÑÐ°ÑÐ¾Ð²Ðµ" â’ "Ð¡Ð°ÑÐ°ÑÐ¾Ð²").
- **Streaming Reliability**: Exponential backoff retry (0.5â’1â’2s + jitter) for Telegram rate-limit errors with adaptive debounce escalation (auto-scales up to 3s).
- **Security & GDPR**: CSRF-protected dashboard authentication, brute-force rate limiting (60 req/min/IP on all API endpoints), API key masking in status endpoints, and Telegram commands for data export (`/mydata`) and deletion (`/deleteme`).

## Non-Goals / Limitations

- **Voice Processing Limitations**: Voice transcription uses `gemini-3.1-flash-lite` â” quality depends on audio clarity and language support of the underlying model. Conversational voice flow requires user confirmation before AI processing.
- **OpenRouter Limitations**: Multimodal detection (images) strictly forces Gemini; OpenRouter is not utilized for vision tasks. **Exception**: Opencode `mimo-v2-omni` natively supports `image_url` in messages and is exempt from the Gemini-only vision redirect.
- **Local Rate Limits**: Heavy request limits are rigidly enforced per user to prevent API quota drain (`MAX_CONCURRENT_HEAVY_REQUESTS`).
- **No ORM**: Raw SQL via asyncpg; no SQLAlchemy or Alembic.

## Architecture

- **Bot Container (`tg-bot`)**: A single async event loop runs both the Telegram webhook updater and the Quart web server via Hypercorn. Webhook mode: Quart receives `POST /webhook/<token>`, deserializes `Update`, and passes it to PTB's internal `concurrent_updates(50)` queue â” the HTTP response returns `200` immediately while processing runs asynchronously. Falls back to long-polling if `WEBHOOK_URL` is unset.
- **Local Bot API Server (`tg-api`)**: Self-hosted `telegram-bot-api` container (`aiogram/telegram-bot-api:latest`) communicates with Telegram via MTProto. The bot sends HTTP requests to `http://tg-api:8081/bot` instead of `api.telegram.org`. Shared Docker volume (`tg-api-data`) enables zero-copy file access for voice/photo/document processing. File limit: 2 GB (vs 50 MB cloud API). Timezone: `Europe/Kyiv`.
- **Media Cleanup Cron (`tg-media-cleanup`)**: Alpine-based sidecar container that runs a 60s-tick loop: (1) `chmod -R g+rX` on the shared volume to fix permission conflicts between `telegram-bot-api` (UID 101) and the bot container (GID 101), and (2) deletes cached media files older than 7 days every 24 hours to prevent disk exhaustion.
- **Database (PostgreSQL)**: Source of truth for users, chats, messages, metrics, roles, and pgvector embeddings.
- **Cache (Redis)**: Optional high-speed layer for caching rate limits and transient states.
- **Third-Party APIs**: Google Gemini (native SDK), Opencode Go (HTTPX), OpenRouter (HTTPX), JINA AI (HTTPX), Tavily (HTTPX).

> **Docker networking:** All three containers share a `tg-net` bridge network. The shared volume `tg-api-data` is mounted at `/var/lib/telegram-bot-api` in both `tg-api` and `tg-bot`. The bot's web server binds to `127.0.0.1:$PORT` on the host (not `0.0.0.0`) â” Caddy/Nginx reverse proxy is expected in front.

```mermaid
graph TD;
    User-->TelegramCloud[Telegram Cloud];
    TelegramCloud-->LocalAPI["tg-api<br/>Local Bot API Server<br/>MTProto â” REST"];
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
| `app/core/`           | Agentic research engine â” multi-step query decomposition and tool use.         |
| `app/context/`        | Context assembly subsystem (assembler, summarizer, token budget).              |
| `app/documents/`      | Document processing: chunking strategies, parsers, document repository.        |
| `app/middleware/`     | Request pipeline middleware (dedup).                                           |
| `app/adapters/`       | Concurrency primitives and Telegram UI adapter.                                |
| `app/db/`             | Database bootstrap: schema validation, migrations runner, RLS, seed.           |
| `app/utils/`          | Shared utilities (formatting, keyboards, background tasks, image utils, reader SSR, etc.). |
| `app/templates/`      | HTML Jinja2 templates for admin dashboard and Telegram Mini App.               |
| `app/games/`          | Crocodile (Charades) game engine: state machine, semantic judge, word bank, judgement cache. |
| `app/bot_instance.py` | PTB Bot singleton â” allows non-PTB code (WebSocket handlers) to call Bot API methods. |
| `app/web_miniapp.py`  | Quart Blueprint for Telegram Mini App (initData auth, memory/settings/game API). |
| `app/deferred_response.py` | Redis-backed deferred AI generation worker for background retry after total outage. |
| `app/intent_router.py`| Lightweight LLM-bypass for weather/currency/crypto queries via WeatherAPI.com, ExchangeRate-API & CoinGecko. |
| `app/state.py`        | In-memory user state management with TTFB stall tracking for network recovery. |
| `docs/`               | Extended architectural documentation.                                          |
| `scripts/migrations/` | Numbered SQL migration files â” single source of truth for all DDL.             |
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

## Setup

1. Clone the repository.
2. Ensure Python 3.14-slim and PostgreSQL (with `pgvector` extension) are installed.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` (if applicable) and fill in necessary configuration.
5. Create PostgreSQL database with `pgvector` extension. Schema is applied automatically on first startup via `scripts/migrations/`.

## Configuration

All configuration is loaded from environment variables (or a `.env` file). Variables are grouped below by functional category.

> [!IMPORTANT]
> Variables marked â… are **required** â” the application will refuse to start if they are absent. Variables marked â  are optional and will use the listed defaults.

---

### ð”  Core / Authentication

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | â… | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` | â” | Obtained from [@BotFather](https://t.me/BotFather). Must match the running bot. |
| `ADMIN_ID` | â… | `6913772015` | â” | Your personal Telegram User ID. Get it via [@userinfobot](https://t.me/userinfobot). Used to gate all `/admin` commands. |
| `ADMIN_SECRET` | â  | Any secure random string, e.g. `openssl rand -hex 24` | â” | Password for the admin web dashboard login form. Also used as encryption seed for stored API keys in the DB. **Keep stable across restarts** â” changing it breaks decryption of stored keys. |

---

### ð—ï¸  Database

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `DATABASE_URL` | â… | `postgresql://user:pass@localhost:5432/gemaibotv2` | â” | Standard asyncpg/libpq DSN. **Must** have the `pgvector` extension available â” the bot won't start without it. For local VPS deployments, use `localhost`; for Supabase use the pooler URL. |
| `DB_POOL_MIN_SIZE` | â  | `2`â“`10` | `2` | Minimum open asyncpg connections. Increase on high-traffic deployments to avoid connection storms. |
| `DB_POOL_MAX_SIZE` | â  | `10`â“`50` | `10` | Maximum open asyncpg connections. Keep below the PostgreSQL `max_connections` limit (default 100). For a 4-vCPU VPS, `20`â“`30` is a safe value. |
| `TEST_DATABASE_URL` | â  | Same DSN format, pointing to a test DB | â” | Used **only** during integration test execution (`pytest -m integration`). Completely isolated from production data. |

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

### ð“¦ Cache & Queue (Redis)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `REDIS_URL` | â  | `redis://localhost:6379/0` or `rediss://user:pass@host:6379` | â” | Used for: distributed LLM semaphores (multi-replica safety), Telegram Mini App Reader page cache (24h TTL), Imagen RPD-per-key counters. If absent, the app falls back to in-process locking (works fine for single-replica deployments). Use `rediss://` scheme for TLS connections. |

---

### ð  Network & Web Server

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `PORT` | â  | `10000` | `10000` | Port the Quart web server and admin dashboard binds to. On VPS, expose via Caddy/Nginx reverse proxy â” do **not** bind directly to `0.0.0.0` in production. |
| `ENABLE_WEB_SERVER` | â  | `true` / `false` | `true` | Disabling this skips starting the Quart server entirely. Set `false` only for local dev without the dashboard. |
| `WEBHOOK_URL` | â  | `https://bot.example.com` | â” | If set, the bot registers itself as a Telegram Webhook at this URL and stops long-polling. **Must be HTTPS.** Required for production webhook deployments. If absent, the bot uses long-polling (simpler for single-server setups). |
| `WEBAPP_BASE_URL` | â  | `https://bot.example.com` | `""` | Public URL from which the Telegram Mini App settings panel and reader are served. If empty, long-response reader falls back to Telegraph links. Must equal `WEBHOOK_URL` in most deployments. |
| `MINIAPP_SHORT_NAME` | No | `gemaibotv2` | `""` | Short name for the Telegram Mini App deep links (`t.me/<bot>/<short_name>`). Set via BotFather: Edit Bot -> Edit MenuButton. |

---

### ð–¥ï¸  Local Bot API Server (Optional)

When configured, the bot communicates with a self-hosted Local Bot API Server instead of `api.telegram.org`. This eliminates network latency for file operations, enables 2 GB file uploads, and provides zero-copy media access via a shared Docker volume.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TELEGRAM_API_ID` | â ï¸  | `12345678` | â” | From [my.telegram.org](https://my.telegram.org). Required only when running the Local Bot API Server container. |
| `TELEGRAM_API_HASH` | â ï¸  | `0123456789abcdef...` | â” | From [my.telegram.org](https://my.telegram.org). Required only when running the Local Bot API Server container. |
| `TELEGRAM_LOCAL_SERVER_URL` | â  | `http://tg-api:8081/bot` | `""` | URL of the Local Bot API Server. When set, enables `local_mode=True` in PTB. When empty (default), the bot uses the official Telegram cloud API. |

---

### ð¤– Gemini Models (Primary LLM Provider)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `GEMINI_API_KEYS` | â… | `key1,key2,key3` | â” | Comma-separated Google AI Studio API keys. The system rotates through them automatically on quota exhaustion or 503 errors. Each key has an independent daily request budget tracked in the DB. Minimum 1 key required. |
| `GEMINI_AVAILABLE_MODELS` | â  | `gemini-2.5-flash,gemini-3.1-flash-lite-preview` | See `config.py` | Controls which Gemini models appear in the `/model` selector for users. If a `DEFAULT_MODEL` is not in this list, it is added automatically with a warning at startup. |
| `DEFAULT_MODEL` | â  | `gemini-3.1-flash-lite-preview` | `gemini-3.1-flash-lite-preview` | Model used for standard conversational messages. Recommended: `gemini-3.1-flash-lite-preview` (fast + cheap) or `gemini-2.5-flash` (smarter, higher cost). |
| `QNA_MODEL` | â  | `gemini-2.5-flash-lite` | `gemini-2.5-flash-lite` | Model used for quick Q&A web search queries (`?` prefix). Optimized for fast factual one-shot answers. |
| `RESEARCH_MODEL` | â  | `gemini-3.1-flash-lite-preview` | `gemini-3.1-flash-lite-preview` | Model used for synthesizing Tavily search results into a final research answer. |
| `URL_SELECTION_MODEL` | â  | `gemini-3.1-flash-lite-preview` | `gemini-3.1-flash-lite-preview` | Lightweight model that scores and filters candidate URLs during agentic web research before full content extraction. |
| `TAXONOMY_MODEL` | â  | `gemini-3.1-flash-lite-preview` | `gemini-3.1-flash-lite-preview` | Model used by MemPalace to classify memories into the Wing/Room taxonomy and to judge temporal contradictions (LLM-as-Judge). Can be set to a cheaper model without quality loss. |

---


---

### Vertex AI (Gemini Enterprise Fallback)

Vertex AI is used as a higher-quota fallback when AI Studio keys are exhausted. When `VERTEX_AI_KEY` and `VERTEX_AI_PROJECT` are set, Vertex is tried automatically.

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
| `OPENROUTER_API_KEYS` | â  | `sk-or-v1-abc,sk-or-v1-xyz` | `[]` | Comma-separated OpenRouter API keys. Required if `USE_OPENROUTER=true`. Rotated same as Gemini keys. Note: OpenRouter is **disabled for multimodal (image) requests** â” Gemini is always used for vision. |
| `USE_OPENROUTER` | â  | `true` / `false` | `false` | If `true`, routes all standard chat, research, and Q&A operations through OpenRouter instead of Gemini. Overrides `DEFAULT_MODEL` etc. with their `OPENROUTER_*` counterparts. Gemini keys are still needed for embeddings and TTS. |
| `OPENROUTER_AVAILABLE_MODELS` | â  | `stepfun/step-3.5-flash:free,qwen/qwen3-4b:free` | `[]` | Models shown in /model selector when OpenRouter is active. |
| `OPENROUTER_DEFAULT_MODEL` | â  | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | Default model for standard chat on OpenRouter. |
| `OPENROUTER_QNA_MODEL` | â  | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for quick Q&A search synthesis. |
| `OPENROUTER_RESEARCH_MODEL` | â  | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for agentic research synthesis. |
| `OPENROUTER_URL_SELECTION_MODEL` | â  | `stepfun/step-3.5-flash:free` | `stepfun/step-3.5-flash:free` | OpenRouter model for URL scoring during agentic research. |

---

### ð”  Search & Web Research

> **Note**: `JINA_API_KEY` is documented in the **Opencode Go Models** section above — it serves dual purpose as both Opencode search grounding and agentic page reader.

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TAVILY_API_KEYS` | â… | `tvly-key1,tvly-key2` | â” | Comma-separated Tavily Search API keys. Used for both the quick `?` search and deep agentic `??` research. Monthly credit budget tracked in the DB with alerting at 97% utilization. |
| `WEATHER_API_KEY` | No | `abc123...` | `""` | API key for WeatherAPI.com. Powers intent-direct weather query handler — weather queries intercepted before reaching the LLM. Without this key they fall through to the LLM. |
| `EXCHANGE_RATE_API_KEY` | No | `abc123...` | `""` | API key for ExchangeRate-API.com. Powers intent-direct currency conversion queries. Without this key currency queries fall through to the LLM. |

---

### ð§  Agentic Research Engine

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `AGENTIC_MODEL` | â  | `gemini-2.5-flash` | `""` (uses `RESEARCH_MODEL`) | Overrides the LLM used inside the agentic research loop. Set to a more capable model (e.g. `gemini-2.5-flash`) for better research quality at higher cost. If empty, falls back to `RESEARCH_MODEL`. |
| `AGENTIC_MAX_ITERATIONS` | â  | `5`â“`15` | `5` | Maximum research loop cycles before the agent is forced to synthesize an answer. Each iteration = one round of query â’ search â’ read â’ reflect. Higher = deeper research, higher API cost. |
| `AGENTIC_MAX_PAGES` | â  | `3`â“`10` | `3` | Maximum web pages the agent reads per iteration. Each page consumes Jina/Tavily credits and LLM tokens. |
| `AGENTIC_MAX_TOKENS` | â  | `100000`â“`500000` | `100000` | Hard token budget cap for the entire agentic session. The loop terminates if accumulated prompt + completion tokens exceed this value. Prevents runaway sessions on complex queries. |
| `AGENTIC_TIMEOUT_SECONDS` | â  | `90`â“`300` | `90` | Wall-clock time limit for the entire agentic session. If the loop doesn't finish within this window, a partial result is returned. Increase to `180`+ on powerful VPS for deeper research. |
| `AGENTIC_PAGE_CONTENT_LIMIT` | â  | `4096`â“`16384` | `8192` | Maximum characters extracted from each web page before truncation. Higher = more context per page, more LLM tokens consumed. |
| `ADAPTIVE_THINKING_ENABLED` | â  | `true` / `false` | `true` | Enables the automatic `thinking_level` selector (14-rule heuristic). When `true`, simple greetings get `low` depth and complex code/research queries get `high`. User's manual `/thinking` setting always overrides this. |

---

### ð“ Rate Limits & Concurrency

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `DAILY_LIMITS` | â  | JSON: `{"gemini-2.5-flash":250}` or compact: `gemini-2.5-flash:250,gemini-2.5-flash-lite:15` | `15` per model | Per-user daily request caps by model name. Requests beyond the limit receive a "quota exceeded" reply. Tracked in the DB with reset at midnight UTC. The TTS model `gemini-2.5-flash-preview-tts` can be included with a separate limit. |
| `MAX_CONCURRENT_HEAVY_REQUESTS` | â  | `4`â“`32` | `4` | Global asyncio semaphore limiting simultaneously active LLM/TTS requests. Backed by Redis for multi-replica safety. On a single-core free container: `4`. On a 4-vCPU VPS with 8 GB RAM: `16`â“`24` is safe (LLM calls are I/O-bound, not CPU-bound). |
| `MAX_CONCURRENT_HEAVY_CALLBACKS` | ? | `4`-`32` | `4` | Separate asyncio semaphore for callback-triggered LLM operations (e.g. inline regeneration, retry buttons). Independent from the main heavy semaphore so callback interactions remain responsive even when the main queue is saturated. |
| `MAX_CONCURRENT_ULTRA_HEAVY_REQUESTS` | â  | `1`â“`8` | `1` | Separate semaphore for agentic research (`??`) sessions. These are memory-intensive due to iterative context accumulation. On a 4-vCPU VPS: `4` is safe. |
| `LRU_STATE_CACHE_SIZE` | â  | `1000`â“`50000` | `1000` | Maximum number of `UserState` objects held in the in-process LRU cache. Each entry is ~2â“5 KB. `1000` was conservatively set for free-tier 512 MB containers. On a VPS with 8 GB RAM, set to `20000`+ to dramatically reduce DB round-trips. |
| `DB_POOL_MIN_SIZE` | â  | `2`â“`10` | `2` | See Database section. |
| `DB_POOL_MAX_SIZE` | â  | `10`â“`50` | `10` | See Database section. |

---

### ð¨ Image Generation

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `IMAGE_MODELS` | â  | `flux,zimage,gptimage,qwen-image,wan-image,klein` | `flux,zimage,gptimage,qwen-image,wan-image,klein` | Pollinations.ai models displayed as buttons in the interactive `/draw` Canvas and exposed in the inline image picker. All 6 models are included by default. Override via env var to restrict the set. |
| `DEFAULT_IMAGE_MODEL` | â  | `flux` | `flux` | Which Pollinations model is pre-selected by default in the Canvas keyboard. |
| `POLLINATIONS_API_KEY` | â  | `pollinations-xxx` | â” | Optional key for Pollinations.ai. Without it, the API works but with stricter rate limits. Get it at [pollinations.ai](https://pollinations.ai). |
| `IMAGE_GEN_DAILY_LIMIT` | â  | `10`â“`100` | `10` | Per-user daily cap for Imagen 4 generations via Google API. Counted separately from Pollinations. |
| `IMAGE_GEN_RPD_PER_KEY` | â  | `25` | `25` | Requests-per-day budget per Gemini API key for Imagen 4. The free tier allows 25 RPD. Tracked in Redis with in-memory fallback. Prevents image quota from consuming keys needed for LLM chat. |
| `IMAGE_GEN_TIMEOUT` | â  | `30.0`â“`120.0` | `60.0` | Max seconds to wait for an Imagen 4 API response before timing out and rotating to the next key. |
| `IMAGE_GEN_MAX_RETRIES` | â  | `1`â“`5` | `3` | Number of Gemini key rotation attempts on quota/error before failing image generation entirely. |

---

### ð” Voice / TTS (ElevenLabs & Gemini)

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `ELEVENLABS_API_KEYS` | â  | `sk_abc,sk_xyz` | `[]` | Comma-separated ElevenLabs API keys used for outbound voice synthesis. Load-balanced with round-robin rotation. If empty, the system falls back exclusively to Gemini REST TTS (`gemini-2.5-flash-preview-tts`). Free ElevenLabs tier gives ~10k chars/month per key. |
| `ELEVENLABS_VOICE_ID` | â  | `XB0fDUnXU5powFXDhCwa` | `XB0fDUnXU5powFXDhCwa` | ElevenLabs Voice ID to use for synthesis. Default is Charlotte (conversational, English/Russian). Browse voices at [elevenlabs.io/voice-library](https://elevenlabs.io/voice-library). |

---

### ð“ Logging & Observability

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `STRUCTURED_LOGGING` | â  | `1` / `true` / `json` | Auto-detected | When set, forces JSON-structured log output instead of plain text. Auto-detection uses `HOSTNAME` length and `PORT` presence as heuristics â” runs structured in cloud, plain locally. Set `1` explicitly to always force JSON. |
| `LOG_PRETTY` | â  | `1` / `true` | `false` | Formats JSON logs with indentation for human readability during local development. Disabling in production reduces stdout volume by ~30%. |
| `HOSTNAME` | â  | Auto-injected by Docker | `unknown` | Docker/Kubernetes injects this automatically. Used as `instance_id` in structured log records for multi-replica tracing. Do not set manually. |
| `SERVICE_NAME` | â  | `gemaibotv2` | `gemaibotv2` | Service label added to every structured log record. Override if running multiple bot instances under different names. |

---

### ð§ª Testing Only

| Variable | Required | Format / Example | Default | Notes |
|---|---|---|---|---|
| `TEST_DATABASE_URL` | â  | `postgresql://user:pass@localhost:5432/test_db` | â” | Postgres connection string used **exclusively** by `pytest -m integration`. Must point to a clean, separate database â” integration tests run destructive DDL and DML. Never set this to the production DB. |

## Run

**Local Python (dev, long-polling):**

```bash
python bot.py
```

**Docker Compose (legacy Northflank, single container):**

```bash
docker-compose -f docker-compose.northflank.yml up -d
```

**Production VPS (3-container stack via GitHub Actions CI/CD):**

The canonical deployment is automated by `.github/workflows/deploy.yml` (triggers on push to `vps_testai` branch). It builds and pushes a Docker image to GHCR, then SSH-deploys 3 containers:

1. **`tg-api`** â” Local Telegram Bot API Server (`aiogram/telegram-bot-api:latest`). Requires `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org). On first deploy, the workflow runs `bot.log_out()` against the Telegram cloud to release the token for local API use (one-time, idempotent via `/opt/tg-local-api-migrated` flag file).
2. **`tg-bot`** â” The Python bot container. Connected to `tg-api` via `TELEGRAM_LOCAL_SERVER_URL=http://tg-api:8081/bot`. Mounts the shared volume `tg-api-data` at `/var/lib/telegram-bot-api` for zero-copy file access.
3. **`tg-media-cleanup`** â” Alpine cron sidecar. Runs `chmod -R g+rX` every 60s (fixes UID 101 permission conflicts) and `find -mtime +7 -delete` every 24h (prevents disk exhaustion from cached media).

All containers share the `tg-net` Docker bridge network and `TZ=Europe/Kyiv`.

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
| `scripts/migrations/000_init_schema.sql` | Complete table definitions (24 tables) â” the full bootstrap DDL |
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
| `app/db/migrations.py` | Migration runner â” applies SQL files with per-file independent transactions and version tracking (`schema_migrations` table). Each file runs in its own transaction; failures are logged and skipped without blocking subsequent migrations. |
| `app/db/schema.py` | Startup validation â” verifies all expected tables exist after migrations |
| `app/db/rls.py` | Row Level Security policy management |
| `app/db/seed.py` | Initial data seeding (admin user, API keys, indexes) |

**Workflow:** On startup, `init_db()` â’ `create_tables()` (validation) â’ `setup_row_level_security()` â’ `run_migrations()` â’ `insert_initial_data()`.

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
| Similarity threshold (floor) | 0.60 (adaptive gap-filter: top â’ 0.15pp, adaptive_floor min 0.40) | `ai_chat.py` / `memory.py` |
| Recall limit | 5 memories (adaptive thresholding filters noise) | `ai_chat.py` `limit` |
| Query expansion model | `gemini-3.1-flash-lite-preview` (~200ms cheap call) | `QUERY_EXPANSION_MODEL` |
| Consolidation model | `gemini-3.1-flash-lite-preview` | `CONSOLIDATION_MODEL` |

**Storage:** Only user intent is embedded (`user_message[:500]`, `source_type='user_intent'`). Bot replies are discarded to maximize vector density. Saving is asynchronous and non-blocking via `submit_retryable()` with 3 retries.

**Retrieval:** Hybrid Reciprocal Rank Fusion (RRF) combining `pgvector` cosine similarity with `pg_trgm` trigram keyword matching (`k=60` smoothing). Falls back to pure semantic search if `pg_trgm` is not installed. Query embeddings use `task_type='RETRIEVAL_QUERY'`.

**Query Intent Gate:** Before performing LLM-based query expansion, a deterministic heuristic (`_should_expand_query`) evaluates whether the user input is a trivial conversational greeting (e.g., "Ð¿ÑÐ¸Ð²ÐµÑ", "Ñ Ð¿Ð°Ñ Ð¸Ð±Ð¾", "ok") or too short (<12 chars) to benefit from keyword enrichment. Trivial inputs bypass the ~200ms Flash-Lite expansion call entirely, saving API quota and reducing latency for ~40% of chat turns. The heuristic is intentionally conservative â” when in doubt, expansion runs.

**Multi-Query Expansion:** When the intent gate passes, vague queries like "that framework I mentioned yesterday?" are rewritten by Flash-Lite into keyword-dense search phrases ("Python FastAPI web framework project") before embedding. This dramatically improves recall for ambiguous references.

**Injection:** Retrieved memories are formatted as XML tags and appended to `system_instruction` (Context Engineering pattern):
```xml
<long_term_memory>
  <fact source="2026-03-20">User prefers Python for backend</fact>
  <fact source="2026-03-18">User works at a fintech startup</fact>
</long_term_memory>
```

**Consolidation:** When raw memories exceed ~8,000 tokens OR 7 days since last consolidation, `gemini-3.1-flash-lite-preview` extracts 5â“8 atomic persona facts. Raw memories are deleted and replaced with consolidated facts (`source_type='consolidated'`) in a single transaction. Consolidation is gated by a debounce (`should_check_consolidation()`) â” checked only every 20th message or every 15 minutes.

### Knowledge Graph Architecture

Relational knowledge is stored as a directed graph in dual tables:

| Table | Purpose |
|-------|--------|
| `memory_nodes` | Entity vertices (name, type, description, `halfvec(768)` embedding) |
| `memory_edges` | Directed relationships (source â’ predicate â’ target, weight, `is_core`, `predicate_embedding`, `source_memory_ids`) |

**Graph Extraction:** During memory consolidation, the LLM extracts subjectâ“predicateâ“object triples from accumulated text. Each triple's subject and object become `memory_nodes`, and the relationship becomes a `memory_edge`. Semantic Entity Resolution (`cosine < 0.12`) merges near-identical entity names at ingestion time to prevent graph fragmentation.

**Semantic Edge Deduplication:** Each predicate is embedded (`predicate_embedding halfvec(768)`). If a new edge's predicate is within cosine distance 0.25 of an existing edge between the same endpoints, the existing edge's weight and `is_core` flag are updated via `ON CONFLICT` upsert instead of creating a duplicate.

**Core Persona Protection:** Edges marked `is_core=TRUE` (name, profession, allergies, permanent identity facts) bypass time-decay entirely in retrieval queries. Once an edge is promoted to core, it stays core (`is_core = memory_edges.is_core OR EXCLUDED.is_core`).

**2-Hop Graph Traversal:** After finding the top-K semantically similar entity nodes, a SQL CTE (`hop1 UNION ALL hop2`) follows outgoing edges from 1-hop neighbours to capture indirect relationships (e.g., asking about "Python" surfaces "FastAPI" via a 2-hop chain). Hop-2 results are labelled `(indirect)` in context.

**Edge Provenance (HippoRAG 2 Dual-Node):** Each `memory_edge` carries a `source_memory_ids BIGINT[]` array linking it back to the original `long_term_memory` rows from which it was extracted. This enables the retriever to surface the full unstructured passage alongside a relevant graph triple, reducing LLM hallucination around short predicates. (Schema ready via migration `027`; retrieval integration is on the Phase 2 roadmap.)

**Scope:** Memory operates globally â” all standard chat messages trigger store/recall when `ltm_enabled=True`. Agentic research (`??`) does not store memories but can recall from them.

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

The application features a heavily engineered test suite (**1813 unit and integration tests, 100% stable CI-ready**) with **parallel execution** via `pytest-xdist`.

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
  - `/start`, `/help` â” Initial onboarding & main menus.
  - `/newchat` â” Reset context and start a fresh conversation.
  - `/model` â” Select the active AI model.
  - `/thinking` â” Configure reasoning depth (Auto/Low/Medium/High).
  - `/res` â” Toggle Deep Research mode (Tavily-powered).
  - `/settings` â” Quick access menu for models, search, and memory toggles.
  - `/stats` â” Personal usage metrics, streaks, and API usage stats.
  - `/documents` â” Manage and query uploaded PDF/DOCX files.
  - `/roles` â” Switch between AI personas/roles. Custom roles support prompt editing (manual replacement or AI-enhanced rewrite with preview and manual tweaking).
  - `/setprompt` â” Set a custom system instruction for the current chat.
  - `/save`, `/conversations`, `/switch`, `/rename`, `/delete` â” Advanced conversation management (persistence).
  - `/export` â” Export the current chat history.
  - `/memory` â” Paginated viewer of long-term memories with per-item inline delete.
  - `/clearmemory` â” Wipe all long-term vector-indexed memories.
  - `/remind` â” Set timed reminders with bilingual time parsing (EN/RU). Supports text, QnA, and agentic AI task delivery.
  - `/draw`, `/img`, `/image`, `/generate` â” Imagen 4 text-to-image generation. Interactive Canvas with aspect ratio and model controls after each image.
  - `/subscribe`, `/unsubscribe` â” Manage hourly intelligence brief subscriptions (LTM-topic-aware web research summaries).
  - `/mydata`, `/deleteme` â” GDPR compliant data export and account deletion.

- **Admin Commands (Requires `ADMIN_ID`):**
  - `/admin` â” Central administration hub.
  - `/listmodels`, `/listusers` â” List configured models and registered users.
  - `/adduser`, `/deluser` â” Manual user management.
  - `/metrics`, `/rolemetrics` â” Detailed system and role-based usage telemetry.
  - `/cachestats`, `/queuestats`, `/docstats`, `/groupstats` â” Performance monitoring for different subsystems.
  - `/clearcache`, `/clearoldmetrics`, `/clearolddocs` â” System maintenance and cleanup.
  - `/updatetavilykeys`, `/checktavilykeys` â” Hot-swap and verify search API keys.
  - `/checkgeminikeys` â” Async parallel health check of all Gemini API keys against the Google API.
  - `/set_inline_thinking <level>` â” Set inline generation `thinking_level` at runtime (stored in `global_settings` DB table, no restart required). Valid: `minimal`, `low`, `medium`, `high`.
  - `/set_provider <name>` - Switch the primary LLM provider at runtime without restart. Valid: `opencode`, `gemini`, `openrouter`. Stored in `global_settings` and takes effect immediately via in-process cache invalidation. Primary tool for live provider failover.
  - `/models` â” Runtime model management wizard. Add, remove, or reset the active Gemini and OpenRouter model lists without container restarts. Changes persist in `global_settings` and sync immediately to all users.
  - `/registergroup` â” Authorize the bot for use in a specific Telegram group.
  - `/reloadconfig` â” Trigger an immediate hot-reload of the environment configuration.

**Web Dashboard (Quart HTTP Routes):**

- `GET /`, `GET /login`, `POST /login`, `GET /logout` â” UI interface (requires `ADMIN_SECRET` authentication and uses Cookie Sessions).
- `GET /health` â” Robust unauthenticated API health check.
- `GET /metrics` â” Exposes Prometheus telemetry text (uptime, errors, usage).
- `GET /api/dashboard` â” Aggregated batch endpoint (replaces 8 individual fetches with 1 RTT). Auth required.
- `GET /api/overview`, `/api/keys`, `/api/errors`, `/api/cache`, `/api/queue`, `/api/database`, `/api/circuit-breakers`, `/api/memory` â” Individual JSON data endpoints for dashboard charts (requires auth cookie or `X-Auth-Token` header).
- `GET /api/key-health` â” Per-key health diagnostics: status, failure count, suspension info. Auth required.
- `GET /api/events` â” Server-Sent Events stream (5s interval) for real-time CPU, memory, DB, queue, and request metrics.

## Main User Flows

- **Inline Mode**
  - _Preconditions_: `/setinline` + `/setinlinefeedback` (100%) configured in BotFather.
  - _Steps_: From any Telegram chat, user types `@gemaibotv2 <query>`. Bot returns 3 tone options + up to 5 image mode buttons (auto-detected via smart routing). User selects tone â’ placeholder with `ð” Ð¸ÑÐµÑ Ð² Ð¸Ð½ÑÐµÑÐ½ÐµÑÐµâ¦` sent to chat. Bot receives `ChosenInlineResult`, runs `_stream_inline_fast()` with **Google Search Grounding** (`enable_web_search=True`) in background via `TaskManager`: `gemini-2.5-flash-lite` with 3 keys racing in parallel. Grounding citations from the winner are captured via `_GroundingMeta` sentinel and appended as an expandable `ð“ ÐÑ ÑÐ¾ÑÐ½Ð¸ÐºÐ¸` blockquote (up to 3 URLs). Image prompts auto-route to the best Pollinations model based on intent (quoted text â’ wan-image, edit verbs â’ klein).
  - _Expected Outcome_: Full AI answer (with real-time web data + citation sources) appears in the original chat, in-place, within ~5â“15 s. On all-rounds failure, user can retry with one tap (`ð” ÐÐ¾Ð²ÑÐ¾ÑÐ¸ÑÑ`, 5-min TTL).
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

- **Logging Variable Chain**: Northflank compose config sets `LOG_JSON=true`, but runtime uses `LOG_FORMAT` environment variable detected in `app/utils/logging_config.py`. The variable `STRUCTURED_LOGGING` referenced in earlier docs is not present in the `Settings` model â” `LOG_FORMAT` is the actual control variable. However, `bot.py:main()` now checks `STRUCTURED_LOGGING` env var directly (alongside `LOG_FORMAT`) for backwards compatibility.
- **OpenRouter Multimodal Capabilities**: OpenRouter is explicitly disabled for multimodal interactions (images); this architectural decision is under-documented in internal application documentation.
- **Healthcheck Start Period**: `Dockerfile.northflank` sets `--start-period=40s` while `docker-compose.northflank.yml` overrides with `start_period: 120s`. The compose value takes precedence in production.
- **CI pytest.ini Comment**: CI workflow (line 74) comments "runs with -m 'not integration' due to pytest.ini" but `pytest.ini` does not set `-m "not integration"` in `addopts`. All tests (unit + integration) run by default; integration tests simply pass without a database.
- **docker-compose.northflank.yml is legacy**: The compose file describes a single-container deployment from the Northflank era. The canonical production deployment is the 3-container stack defined in `.github/workflows/deploy.yml`. The compose file is retained for backwards compatibility but does not include the Local Bot API Server or media cleanup sidecar.

## Architecture Decisions

Key implementation decisions that frequently need re-discovery:

### State Persistence
`UserState` lives in an in-memory `LRUCache` (configurable via `LRU_STATE_CACHE_SIZE`, default 1000). Changes are debounced to PostgreSQL with a 300ms window via `_pending_persists` dict, preventing DB write storms during rapid interactions.

### Error Tagging
Telegram messages carry invisible `ErrorCode` tags via zero-width space characters (`\u200b` + enum value). This enables O(1) error classification from user-visible messages without fragile text/emoji parsing. See `tag_error()` and `classify_from_message()` in `app/errors.py`.

### Streaming Message Overflow
When a streaming AI response exceeds Telegram's ~4000 char limit, `StreamingWriter` finalizes the current message and creates a new one via `reply_new_message()`. The split preserves markdown continuity: `_detect_open_markdown()` closes unclosed code blocks/bold/italic in the frozen message and reopens them in the continuation.

### Memory Consolidation Triggers
Consolidation fires when raw memories exceed ~8,000 tokens OR 7 days since last consolidation. A debounce gate (`should_check_consolidation()`) prevents DB queries on every message â” it checks only every 20th message or every 15 minutes.

### Singleton Lifecycle
`DatabaseManager`, `ProviderRouter`, and `PromptRegistry` use lazy-init singletons. Tests handle cleanup via `conftest.py` fixtures. Import-time side effects are avoided by deferring initialization to first access. The image-processing worker pool in `app/utils/image_utils.py` also follows this rule as of `v2.12.11`: the `ProcessPoolExecutor` is created only on first real image work, so importing Gemini / multimodal / audio compatibility modules remains safe in restricted test and CI environments.

### Key Rotation Health Scoring
API keys are selected by a two-tier SQL query: first from active keys that haven't exceeded failure thresholds, then from keys whose cooldown period has expired. Per-key health data (failure count, last failure time, suspension reason) is tracked in `repos/keys.py`. The Crocodile judge path participates in the same lifecycle: failed judge requests suspend the offending Gemini key, and successful judge winners explicitly record recovery so transiently unhealthy keys can re-enter the active pool.

## Future / Roadmap

| Feature | Description | Status |
|---------|-------------|--------|
| **Debate Mode** | Multi-model argument synthesis â” the bot queries 2â“3 models with opposing viewpoints, then synthesizes a balanced answer highlighting agreements, disagreements, and confidence levels. Ideal for complex or controversial topics. | Planned |
| **Shared Group Brain** | Group-level long-term memory â” when the bot is added to a Telegram group, it builds a shared LTM across all group members. Group memories are tagged by contributor and searchable by any member. Includes configurable privacy controls (opt-in/opt-out per user). | Planned |

## Testing

The test suite is organized into three levels:

```
tests/
â”â”â” conftest.py                   # Global fixtures: DB cleanup, async exception surfacing
â”â”â” factories.py                  # Telegram object builders (Update, Message, Context, User)
â”â”â” integration/
â”   â”â”â” conftest.py               # Transactional DB fixtures (asyncpg rollback per test)
â”   â””â”â” test_e2e_app_smoke.py    # Integration: real DB, mocked LLM network
â”â”â” e2e/
â”   â”â”â” test_chat_happy_path.py  # Full pipeline: handle_request â’ stream â’ DB persist
â”   â””â”â” test_stream_recovery.py  # Mid-stream APIError / timeout recovery
â”â”â” test_error_codes.py          # ErrorCode tagging, typed/HTTP/string classification
â”â”â” test_factories.py            # Factory correctness (structure, independence)
â”â”â” test_formatting.py           # TelegramFormatter, escape_format_chars (AAA)
â”â”â” test_semaphore_invariants.py # GlobalLLMSemaphore: release on success/exception/cancel
â”â”â” test_streaming_writer.py     # StreamingWriter: write/finalize, rate-limit retry, overflow
â”â”â” test_text_format_aaa.py      # markdown_to_html, sanitize_html_tags, split_text_safe
â””â”â” test_thinking_classifier.py  # classify_thinking_level, resolve_thinking_level
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
- All tests follow **Arrange-Act-Assert** strictly â” one behaviour per test function.
- Integration tests use **transactional rollbacks** (`asyncpg`) for full isolation.
- External AI/Telegram API calls are replaced by **fake adapters** and **async generator stubs**.
- `GlobalLLMSemaphore` uses the local asyncio fallback path (Redis patched to `None`).

## Contributing

1. Create a descriptive PR.
2. Verify all `pytest` checks pass: `python -m pytest tests/ --override-ini="addopts="`
3. Run `ruff check app/ tests/ --output-format=concise` â” zero violations required.
4. Run `mypy app/ --ignore-missing-imports` â” exit 0 required.

## License

MIT (Verified via shield badge notation in legacy files).
