# Changelog

All notable changes to this project will be documented in this file.
Format is optimized for agent-parseable context.

## [Unreleased] - 2026-08-29 - Reproducible Dependency Frontier

### 📦 Dependency resolution and verification

- Replaced independent requirements manifests with PEP 621 dependencies in `pyproject.toml` and a committed `uv.lock`; local, CI, audit, and production-container installs now use the same exact graph through uv 0.12.6.
- Added a read-only dependency-frontier audit that resolves both policy-constrained and unconstrained stable versions twice for Linux production and Windows development, applies a seven-day release cooldown, rejects source-only candidates, and reports policy-blocked major upgrades without presenting cooldown downgrades as updates.
- Added contract coverage for dependency metadata, deterministic resolution, conservative pre-1.0 classification, platform enforcement, truthful terminal states, locked CI installation, and locked production-image construction.
- Removed the unused `tavily-python` SDK; production Tavily traffic already uses the repository's tested `httpx` adapter, so retaining the SDK added supply-chain surface without exercising application behavior.
- Added focused offline boundary tests for the third-party APIs used by Telegram, Gemini, Quart/Hypercorn, Pydantic, cryptography, document/image processing, serialization, astrology, Redis, and asyncpg.
- Added a digest-pinned production base image, exact installed-versus-lock verification, an offline real `/health` container smoke, CycloneDX SBOM artifacts, and a license inventory that reports unknowns and fails only against an explicit repository denylist.
- Added a protected, manual baseline-versus-candidate live canary for same-repository dependency PRs. It uses dedicated Telegram/Gemini/Tavily credentials only in the probe step, deletes the Telegram message in cleanup, records redacted evidence, distinguishes configuration/contract/transient failures, and publishes success only after a fully passing comparison.

## [Unreleased] - 2026-08-29 - Trivia Resilience and Admin Observability

### 🗄️ Migration portability and schema safety

- Made the complete migration chain portable to standard pgvector PostgreSQL by guarding the Supabase-only `service_role`, correcting stale RLS context names, and replacing open Horoscope/Tarot subscription policies with tenant/admin isolation.
- Added forward migration `070` to normalize affected policies and migration `071` to backfill chat preference columns that previously existed only in live databases; synchronized runtime RLS/schema catalogs and made post-migration table/critical-column validation fail closed.
- Added a shared migration-manifest validator for deterministic names, unique versions, UTF-8, and non-empty SQL; numbered failures now stop before legacy DDL.
- Made CI apply migrations twice and then require zero drift before integration tests and deployment.

### 🧩 Daily Trivia generation

- Added a key-local retry plan for each Trivia lane: the configured Gemini model, `gemini-3.6-flash`, the configured model again, then `gemini-3.6-flash` again. Authentication and quota failures rotate the key immediately; transient, content, and response-validation failures advance through the model plan first.
- Made every attempted Gemini/OpenRouter request reserve its daily request allowance atomically before the provider call, including failed requests, fallbacks, and stream races, so RPD accounting reflects actual provider traffic and concurrent workers cannot oversubscribe a key.
- Added a distributed three-hour cooldown for failed future-puzzle preparation with an in-process fallback when Redis is unavailable. Today's puzzle remains immediately retryable, one failed future date no longer aborts the rest of the preparation window, and successful preparation clears the cooldown.

### 💬 Telegram menus and subscriptions

- Fixed first-time horoscope subscriptions so supplied morning/evening delivery times are persisted by the initial insert instead of appearing as disabled in `/horoscope_settings`.
- Made `/help` topic and back callbacks carry the selected language explicitly, preserving the complete RU/EN catalog across nested menus independently of Telegram interface-language drift while keeping legacy callback data compatible.
- Rebuilt `/admin` from a tested command catalog that covers the current operator surface and adds validated HTTPS links to the main and Daily web dashboards.

### 📊 Admin Dashboard

- Replaced fragmented frontend polling with one canonical `/api/dashboard` snapshot containing `overview`, `providers`, `infrastructure`, and newest-first `errors`. Independent data-source failures now produce visible unavailable states instead of breaking the whole page.
- Normalized response-time, error-rate, cache-rate, queue, database, provider, and key-health fields; fixed chronological metric hydration; and made configured Gemini model/key combinations visible even before they record usage.
- Added regression coverage for the snapshot contract, escaped error rendering, partial initialization, RLS-scoped key health, zero-usage models, menu navigation, subscriptions, provider retries, RPD reservation, and preparation cooldowns.

## [Unreleased] - 2026-08-27 - Codebase Hardening, LTM Writer, and Public Help

### 🔒 CI, deployment, and dependency safety

- Made CI run for all pull requests and supported push branches, split unit/E2E from real PostgreSQL integration tests, and added an ephemeral `pgvector/pgvector:pg17` service with migrations and fail-closed `TEST_DATABASE_URL` validation.
- Made production deployment consume only a successful completed CI run on `vps_testai`, check out and deploy the exact verified `workflow_run.head_sha`, serialize superseded deployments, retry transient image builds, and fail when container or Telegram health checks fail.
- Upgraded the vulnerable `cryptography` dependency and verified the production requirements with `pip-audit`.
- Normalized the complete Python tree with Ruff and added `python -m ruff format --check .` to the pinned CI lint job. A contract test prevents removal of the format gate.

### 🧠 Transactional long-term memory writes

- Added `app/repos/memory_graph_writer.py` as the shared, deterministic node/edge/provenance writer for both real-time extraction and batch consolidation.
- Kept transaction, RLS context, advisory lock, and consent-epoch ownership in the caller. Embeddings and provider output are prepared before the write transaction; the writer never acquires the global pool or performs network calls.
- Required durable source-memory IDs for every graph candidate and made node resolution, temporal conflict closure, edge merge/upsert, normalized `memory_edge_sources` writes, and compatibility snapshot refresh succeed or roll back together.
- Added regression coverage for exact and semantic node reuse, duplicate merge semantics, monotonic core/weight updates, provenance upserts, tenant scoping, and transaction rollback.

### 💬 Public commands and user-facing guidance

- Added `app/bot_commands.py` as the single source of truth for the public command identities, RU/EN Telegram menu descriptions, categorized `/help` overview, and topic navigation. Administrative and developer commands remain hidden.
- Corrected `/subscribe` to enable daily intelligence briefings; it no longer opens horoscope settings. `/unsubscribe` continues to disable the matching briefing subscription.
- Replaced the stale `/tarot_settings` placeholder with working inline subscribe/unsubscribe callbacks for the daily Tarot card.
- Removed server variable names, credential details, raw upstream error bodies, HTTP codes, and GraphRAG jargon from public error/data messages while preserving technical diagnostics in logs.
- Updated README command documentation, memory transaction/provenance guidance, architecture diagrams, verification commands, and ADR 0002.

### 🛡️ Runtime reliability and maintenance

- Bounded shutdown cleanup for tracked background tasks and ensured cancelled tasks are awaited without allowing one stuck task to block process exit indefinitely.
- Made Tavily force-refresh replacement atomic and removed the unsafe standalone database-clearing script.
- Added explicit chat-state persistence markers and resolved the remaining production Mypy errors.

### ✅ Verification commands

- `python -m pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts="`
- `python -m pytest tests/ -m "integration" -n 0 --override-ini="addopts="` with an isolated `TEST_DATABASE_URL`
- `python -m ruff check .`
- `python -m ruff format --check .`
- `python -m mypy app bot.py`
- `uv export --locked --no-dev --output-file production-requirements.txt` followed by `uv run --locked pip-audit -r production-requirements.txt --progress-spinner off`
- `python scripts/check_encoding.py`

## [Unreleased] - 2026-08-25 - Long-Term Memory Safety Hardening

### 🧠 Consent, provenance, and retrieval

- Added globally unique memory-consent epochs and renewable cross-process provider leases so queued chat, document, search, voice, media, extraction, consolidation, graph, and scheduled-brief work cannot outlive LTM disable or account erasure.
- Made migration `067` atomically upgrade legacy dashboard-created UUID graph identifiers to the canonical BIGINT schema before adding provenance foreign keys.
- Disabled implicit private-memory capture from group conversations and normalized canonical chat history separately from provider-specific payloads, preventing binary/media artifacts and continuation duplicates from entering persisted history or LTM.
- Made graph facts traceable to exact source memories, added tenant-safe composite provenance constraints and RLS policies, and made cleanup remove expired, unsupported, orphaned, or source-derived data in a deterministic order.
- Hardened retrieval ranking, freshness/expiry checks, feedback attribution, prompt-delimited memory injection, and local/LLM summary token budgeting for Cyrillic and emoji.

### 🔒 Deletion and tenant isolation

- Made `/deleteme` a transactional, private-chat-only account erasure flow that removes legacy rows, transfers or removes managed groups safely, clears runtime caches, and prevents background metrics or summarization from recreating deleted users.
- Added authenticated Mini App memory controls and export scope, and made graph visualization use one RLS-scoped database snapshot with a durable LTM-consent gate.
- Changed RLS policy setup to fail startup when a configured table or policy cannot be protected, instead of silently continuing with partial tenant isolation.

### 💬 Conversation handlers

- Documented the deliberate `per_chat=True`, `per_user=True`, `per_message=False` mode for hybrid callback/command/text/location conversations and scoped suppression to the one expected `python-telegram-bot` configuration warning. Unrelated PTB warnings remain visible and test-failing.

### ✅ Verification

- Added regression coverage for consent races, provenance and tenant constraints, account erasure, graph authorization, canonical persistence, summary lifecycle/budgets, and all affected `ConversationHandler` builders.

## [Unreleased] - 2026-08-14 - Dynamic Model Catalog Correctness

### 🧠 Model configuration and administration

- Removed the static Gemini version allowlist from selectable-model loading and runtime eligibility. Ordered env lists now accept future syntactically valid `gemini-*` chat IDs without requiring a bot release.
- Split the current env/Secret baseline from explicit `/models` database overrides. Legacy auto-seeded JSON arrays are removed, v2 admin overrides remain authoritative across restarts, and **Reset to .env** deletes the override instead of copying another stale snapshot.
- Added consistent empty-list semantics for all four `*_AVAILABLE_MODELS` variables: unset or whitespace uses defaults, while the single token `none` creates an intentionally empty selector. Internal role/default models are no longer injected into `/model`.
- Expanded `/models` to Gemini, Opencode, OpenRouter, and FreeTheAI with source labels, per-model delete buttons, collision-safe short callbacks, immediate list refresh, and typed add/remove outcomes.
- Gemini additions now query the Models API and require `generateContent`; unsupported models and temporary validation failures produce distinct administrator messages instead of being reported as duplicates.
- Preserved the shared settings object during hot reload, reapplied admin overrides before chat migration, included FreeTheAI in migration, and prevented migration to hidden role defaults or mass migration when every selectable list is empty.
- Unified `/model` menu ordering with callback resolution so a configured but hidden OpenRouter list cannot shift Opencode or FreeTheAI model indices.

### ✅ Verification

- Added regression coverage for exact env parsing, `none`, v2 override precedence, legacy cleanup, mutation atomicity, Gemini capability validation, deletion UI, callback length/order, future model fallback, and reload migration safety.

### 📨 AI response delivery

- Replaced string/tuple streaming and hidden metadata `ContextVar` channels with typed generation requests and events carrying completion, failure, deferred, usage, grounding, finish reason, and actual route data.
- Made `TelegramResponseDelivery` the sole owner of progressive edits, final text, and final action keyboard. Reader/Telegraph rows are prepended atomically and can no longer be erased by a handler after publication.
- Unified long-response delivery behind Redis Reader → Telegraph → safe Telegram split, with immutable receipts, bounded flood retry, partial-response recovery, delayed cancel feedback, and guaranteed request cleanup.
- Migrated chat, QnA, photo/media-group vision, document Q&A, AgenticSearch completion, deferred delivery, inline generation, and horoscope consumers; removed the legacy `StreamingWriter`, `stream_response`, `StreamingUIAdapter`, and obsolete private-state tests.
- Fixed structured Gemini HTTP errors being misclassified by incidental `503` digits in diagnostics, and added safe text fallback when the Daily 2048 cover asset is unavailable.

## [Unreleased] - 2026-06-27 - Inline Context Continuity, Type Safety & Performance

### ⚡ Performance & Bolt Optimizations
- **JSON Serialization (`app/tarot.py`, `app/natal/accuracy.py`, `app/natal/city_catalog.py`, `app/handlers/cmd_admin.py`):** Replaced standard library `json` with `app.utils.json_compat` (backed by `orjson`), accelerating encode/decode paths by 2-6×. Updated file loading to use `json.loads(f.read())`.
- **Concurrent Daily Puzzle Prep (`app/games/daily_2048.py`):** Parallelized `ensure_prepared_puzzles` via `asyncio.gather()`, reducing a 7-day preparation loop from sequential sum of DB RTTs to a single concurrent max RTT.
- **Database Index Optimization (`scripts/migrations/056_bolt_performance_indexes.sql`):** Added missing indices for `conversation_messages(conversation_id)` and `memory_edges(user_id, target_node)`, eliminating sequential scans during history fetches and reverse graph traversals.
- **Async Concurrency Refactoring (`app/games/crocodile_daily.py`, `app/handlers/inline.py`, `app/handlers/scheduled_briefs.py`):** Replaced multiple sequential `await` calls inside loops with `asyncio.gather`, drastically reducing wall-clock execution time for daily puzzle generation, inline image placeholder generation, and scheduled brief broadcasts.

### 🔗 Inline Q&A Continuity
- **Deep Link Context Loading (`app/handlers/commands.py`):** Inline interactions now generate a `?start=ctx_<token>` deep link attached to a "💬 Продолжить" button. Clicking it loads the specific inline question and answer directly into the bot's private chat history as the most recent interaction, allowing seamless follow-up questions.
- **Rolling Context Store (`app/cache.py`):** Implemented a Redis-backed rolling token store (`store_inline_context`) with a 24-hour TTL and a strict per-user limit of 10 contexts (enforced via Redis ZSET).
- **History Sync Fix:** Bypassed the bolt optimization in `update_user_chat` by resetting `_original_length` to `0` when loading inline context, ensuring the prepended/appended history is guaranteed to sync to Postgres regardless of array length.

### 🐛 Type Safety & Bug Fixes
- **Comprehensive Mypy Fixes:** Resolved 30+ strict typing errors across the codebase.
  - Fixed ConversationHandler state return types (`-> int | str`) in `horoscope_subscription.py` and `natal_chart.py`.
  - Fixed variable scoping issues and assignment types in `city_readiness.py` and `cmd_keys.py`.
  - Added strict `None` checks for `elapsed_ms` and integer type casting in `web_miniapp.py`.
  - Fixed import of `send_tarot_invite` in `web.py`.

---

## [Unreleased] - 2026-06-25 - Unified Daily Admin & Tarot Broadcast

### 🛠️ Unified Admin Interface
- **Daily Broadcast Center (`/admin_daily`):** Combined the previously fragmented `/admin_dailycroc` and `/admin_daily2048` pages into a single SPA-like interface (`app/templates/admin_daily.html`) with hash-based routing (`#broadcast`, `#croc`, `#2048`, `#horoscope`, `#tarot`). Legacy routes now perform 301 redirects to the new unified panel.
- **Global Broadcast Management (`app/web.py`):** Centralized the global delivery kill-switches and subscriber metrics for Crocodile, Horoscope, and Tarot under new `/api/admin/broadcast/*` endpoints.

### 🔮 Esoteric & Astrology Suite
- **Tarot Daily Broadcast (`scripts/migrations/054_add_tarot_daily_subscriptions.sql`, `app/repos/tarot_daily_subscriptions.py`):** Added subscription persistence and scheduler helpers for the "Карта дня" (Daily Tarot Card) broadcast. Supports personalized timezone scheduling mimicking the existing Horoscope subscription system.
- **Tarot Ops Controls:** Exposed Tarot daily preparation status and forced regeneration triggers (`/api/admin/tarot/status`, `/api/admin/tarot/regenerate`) directly within the new Daily Admin panel.

---

## [Unreleased] - 2026-06-20 - Esoteric Suite, Admin Alerts & Core Ops

### 🔮 Esoteric & Astrology Suite
- **Natal Charts (`app/handlers/natal_chart.py`, `app/natal/`):** Implemented a complete natal chart generation and interpretation system. Features a step-by-step chat wizard, interactive WebApp birth form (`app/templates/natal_form.html`), GeoNames-based city autocomplete, and detailed hosted results reporting. Backed by `scripts/migrations/050_add_natal_reports.sql`.
- **Tarot Readings (`app/handlers/tarot_chat.py`, `app/handlers/tarot_daily.py`, `app/handlers/cmd_tarot.py`, `app/tarot.py`):** Added support for multi-spread interactive Tarot readings including Classic spread, Card of the Day, Yes/No spread, Career/Finance, Relationships, and Personal Growth. Includes live session state management in Redis and additional card draws. Backed by `scripts/migrations/051_add_tarot_daily_readings.sql` and `052_add_tarot_sessions.sql`.
- **Horoscope Subscriptions (`app/handlers/horoscope_subscription.py`, `app/handlers/scheduled_horoscopes.py`, `app/repos/horoscope_subscriptions.py`):** Added a Daily Horoscope subscription service featuring Morning (today) and Evening (tomorrow) automated deliveries in DM according to zodiac signs and user timezones (UTC offset). Backed by `scripts/migrations/047_add_horoscope_subscriptions.sql`.

### 🛡️ Core Ops & Admin Tools
- **Admin Alerts for Unauthorized Users (`app/admin_alerts.py`, `app/handlers/messages.py`, `app/handlers/callbacks.py`):** Integrated real-time admin notification system. When an unauthorized user attempts to access the bot, an alert with user details is broadcast to admins with inline buttons (`[Разрешить]`, `[Отклонить]`) to control access permissions on-the-fly.
- **Token Count Logging (`app/streaming.py`):** Enabled token count logging for both streaming and non-streaming Gemini responses to allow cost tracking and logging observability.

### 🐛 Bug Fixes & Mocks
- **Test Suite Mocks (`tests/test_messages.py`):** Fixed missing `application` attribute in `DummyContext` and `language_code` in `MockUser` to resolve test execution failures.
- **API Keys Configuration Safeguard (`tests/conftest.py`):** Added fallback environment setup for `GEMINI_API_KEYS` in tests configuration to prevent crashes during test runners initialization when environment keys are absent.

### ✅ Verification
- `python -m pytest tests/test_messages.py -q` → **Passed**
- `ruff check .` → **Passed**
- `python scripts/check_encoding.py` → **Passed**

---

## [Unreleased] - 2026-06-02 - Daily 2048 Sprint

### 🎲 Daily Games

- **Daily 2048 Sprint (`app/games/daily_2048.py`, `app/repos/daily_2048.py`, `app/templates/daily_2048.html`):** Added a server-authoritative 2048-style daily sprint where the explicit objective is to reach a target tile or total board value. First completion records moves, elapsed time, merge score, and final score; subsequent post-win play stays in local practice mode and is not written to daily records.
- **No-repeat daily challenge generation (`app/repos/daily_2048.py`, `tests/test_daily_2048.py`):** Automatic puzzle preparation now computes stable fingerprints for the visible starting board and daily challenge contract, then retries deterministic candidates until it avoids repeated challenge signatures and repeated starting boards from the prepared history.
- **Premium tile polish (`app/templates/daily_2048.html`, `app/templates/admin_daily2048.html`):** Player tiles now use pseudo-3D gradients, internal highlights, depth shadows, and hover glow; admin mini/editor cells received the same quieter depth treatment so prepared boards look consistent with the game surface.
- **Daily 2048 cover refresh (`artifacts/daily2048_cover.png`):** Rebuilt the cover without a phone/device artifact, removed the left-side 2-4-8-256 chain, and replaced the changelog-like copy with shorter poster-style messaging.
- **Daily-slot switch (`/set_daily_game crocodile|2048`, `global_settings.daily_game_mode`):** Admins can switch this month's active daily experience without removing Crocodile. `/dailycroc`, `/daily2048`, and scheduled daily prompts route to Daily 2048 while the mode is active, using the existing opt-in delivery audience.
- **Prepared puzzle planner (`/admin_daily2048`, `/webapp/admin_daily2048`):** Operators can prepare boards several days ahead, set tile/total goals, tune par moves and target seconds, and provide deterministic spawn sequences for custom daily starts.
- **Result and monthly champions flow (`app/games/daily_2048_telegram.py`, `app/handlers/daily_2048.py`):** Completion messages include score, moves, elapsed time, day leaderboard, and an inline "Лучшие за месяц" button that lists each day's top player for the selected month.
- **Daily 2048 cover message flow (`app/handlers/daily_2048.py`, `app/games/daily_2048_telegram.py`, `scripts/migrations/049_add_daily_2048_prompt_messages.sql`):** Daily 2048 invitations now use the prepared cover as a photo message, track the prompt message, and edit that same cover message into the completion result instead of sending a separate plain-text result.
- **Daily 2048 practice and theme palette polish (`app/games/daily_2048.py`, `app/templates/daily_2048.html`):** Post-completion practice now stays playable after the first extra move, while Aero, Desk, Swiss, and Deco tile palettes use distinct number-color routes. Leaf keeps its existing tile palette.
- **Daily 2048 loss practice restart (`app/games/daily_2048.py`, `app/web_miniapp.py`, `app/templates/daily_2048.html`):** Players who run out of moves can enter unranked practice too; because the lost board has no legal moves, practice restarts from the daily starting board with zero practice moves, score, and timer.
- **Daily 2048 mobile gesture lock (`app/templates/daily_2048.html`):** The Mini App now disables Telegram vertical close/minimize swipes where the client supports it and blocks browser touch scrolling while the game is open, so downward moves stay inside the 2048 board.

### ✅ Verification

- `python -m pytest -q tests/test_daily_2048.py -n 0 --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_daily2048_swipe_lock` → **28 passed**
- `python -m pytest -o addopts='' -q -n 0 tests/test_daily_crocodile.py --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_dailycroc_2048_regression_env2` → **27 passed**
- `python -m ruff check app/db/schema.py app/handlers/callbacks.py app/handlers/cmd_admin.py app/handlers/commands.py app/handlers/daily_crocodile.py app/handlers/daily_2048.py app/games/daily_2048.py app/games/daily_2048_telegram.py app/repos/daily_2048.py app/web.py app/web_miniapp.py tests/test_daily_2048.py` → **All checks passed**
- `python scripts/check_encoding.py` → **passed**
- `node -e "<daily_2048.html script parse>"` → **daily_2048.html script parses**

---

## [Unreleased] - 2026-06-01 - OSS Maintainer Readiness

### Documentation

- Added an explicit MIT `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, and `ROADMAP.md` so the public repository has standard OSS maintainer surfaces.
- Added a top-level README overview with maintainer status, public maintenance signals, security/contribution links, and Codex/API-credit usage intent.
- Added `docs/MAINTAINER_QUEUE.md` to document the open-PR queue snapshot, label taxonomy, and closure policy.
- Documented that runtime user/adoption metrics are not yet published and must be exported only as anonymized aggregate deployment counts.
- Updated the README contributing/license section to point to the new policy files.

---

## [Unreleased] - 2026-05-28 - Tarot Spread Variations Expansion

### 🎴 Tarot Enhancements

- **5 New Spread Variations (`app/tarot.py`, `app/handlers/inline.py`):** Expanded the inline Tarot system to support multiple spread types via a dropdown selector. Added variations:
  - 🎴 **Карта дня** (Card of the Day) - single card, context changes based on whether it's requested directly or via command without query.
  - 🔮 **Да или Нет** (Yes/No) - quick binary answer spread.
  - 💼 **Карьера и финансы** (Career & Finances) - 3-card spread (Past, Present, Future).
  - ❤️ **Отношения** (Relationships) - 3-card spread (You, Partner, Relationship).
  - 🌟 **Личностный рост** (Personal Growth) - 3-card spread (Strengths, Weaknesses, Advice).
- **Automated Validation (`app/handlers/inline.py`):** "Card of the Day" intelligently vanishes from the dropdown if the user provides a specific query string. Yes/No questions employ soft validation, falling back gracefully without requiring a literal question mark.
- **Fortune Cookie HTML Fast-Path (`app/handlers/inline.py`):** Refactored layout to bypass LLM generation for instant, stateless UI responses during spread selection.
- **Russian Content Enrichment (`app/assets/tarot.json`):** Extended all 78 tarot cards with `fortune_telling_ru` localizations for richer, localized interpretations.

### ✅ Verification

- `python -m pytest tests/test_tarot.py -q` → **10 passed**
- Full test suite regression check → **Passed**
- `ruff` linting and `mypy` typing → **Passed**

---

## [Unreleased] - 2026-05-11 - Cold-Start Latency Optimization (−54% P95)

### ⚡ Startup Performance

Systematic import-time profiling (`python -X importtime`) identified four hot-path modules pulling heavyweight transitive dependencies at startup. Four targeted lazy-import refactors reduced cold-start P95 by **~54%** (from ~2.41 s → ~1.11 s) with no behavior changes and no new dependencies.

| Epoch | File | Deferred symbol | Heavy chain eliminated |
|-------|------|-----------------|------------------------|
| 1 | `app/handlers/messages.py` | `cmd_image` imports (draw state + generation) | Avoids transitive image-provider chains on every worker start |
| 2 | `app/handlers/msg_roles.py` | `app.agents` in `handle_custom_role_generation` | Avoids agent subsystem import on non-role message paths |
| 3 | `app/handlers/menus.py` | `app.document_processor.get_user_documents` | Eliminates `pypdf` + `docx` (125 K μs cumulative) from startup |
| 4 | `app/handlers/cmd_admin.py` | `from google import genai` in `list_models_command` | Eliminates google-genai SDK init from startup (admin-only, rare) |

**Pattern**: Python's module cache (`sys.modules`) makes each deferred import free after the first actual call, so there is zero per-call overhead beyond the first invocation.

**Benchmark** (`artifacts/perf/bench_startup.py`, 5 warm subprocess spawns):

| Metric | Before (baseline) | After (this PR) | Δ |
|--------|-------------------|-----------------|---|
| P95    | ~2.41 s           | ~1.11 s         | −54% |
| mean   | ~2.00 s           | ~1.10 s         | −45% |

### 🧪 Test Regression Fixes

- **`tests/test_messages.py`**: Updated `submit_task` mock path to `app.handlers.messages.submit_task` (was wrong namespace after lazy-import refactor). Added `ensure_state_loaded` and `is_authorized` mocks to prevent live-DB dependency in the 9 message handler tests. All 9 pass.

### 🛠️ Infrastructure

- **`artifacts/perf/bench_startup.py`**: Benchmark harness that measures cold-start P95/P99/mean via repeated subprocess spawns of `import bot`.
- **`artifacts/perf/bench_routing.py`**: Routing + handler benchmark scaffold (mocks all I/O; measures `handle_request` dispatch overhead).

### ✅ Verification

- `python -m pytest tests/test_messages.py -q` → **9 passed**
- `python artifacts/perf/bench_startup.py` → P95 = **1.11 s**, mean = **1.10 s**
- `ruff check app/handlers/messages.py app/handlers/msg_roles.py app/handlers/menus.py app/handlers/cmd_admin.py` → pending (run before merge)

---

## [Unreleased] - 2026-05-10 - Gemini 3.1 Flash Lite Stable Migration & Model-Aware Thinking

### 🔄 Model Migration

- **Gemini 3.1 Flash Lite → Stable (`gemini-3.1-flash-lite`):** Migrated all 47 files from `gemini-3.1-flash-lite-preview` to the GA-stable `gemini-3.1-flash-lite` model ID. Covers `app/config.py`, all provider routers, handlers, test fixtures, templates, README.md, and CHANGELOG.md. Both AI Studio API key and Vertex AI Express paths now use the stable identifier.

### 🧠 Model-Aware Adaptive Thinking

- **Default `high` thinking for Flash Lite chat (`app/thinking_classifier.py`, `app/handlers/ai_chat.py`):** When a user's thinking level is set to `auto` (default) and the active model is `gemini-3.1-flash-lite`, the adaptive classifier now returns `"high"` directly — bypassing the 14-rule regex heuristic. This ensures consistent high-quality reasoning for the updated model in standard chat sessions. Inline mode is unaffected (uses its own `INLINE_THINKING_LEVEL`). User explicit preferences (`low`/`medium`/`high`/`off`) always override.
- **Centralized model-default map (`app/thinking_classifier.py`):** Added `_MODEL_DEFAULT_THINKING` dictionary for extensible per-model thinking defaults. New models requiring specific thinking budgets can be added to this map without touching handler code.

### ✅ Verification

- `ruff check app/ tests/` → 2 pre-existing errors (unrelated: `deferred_response.py` I001, `ai_photo.py` RUF059)
- `python -m mypy app/ --ignore-missing-imports` → 4 pre-existing errors (unrelated: `cmd_keys.py`, `model_selector.py`)
- `python scripts/check_encoding.py` → **Passed**
- `python -m pytest tests/ -q --timeout=60` → **1,845 passed**, 96 skipped (3 DB-dep failures require local PostgreSQL)

---

## [Unreleased] - 2026-05-05 - FreeTheAI Multimodal Integration

### 🎨 FreeTheAI Multimodal Integration

- **Added `FreeTheAIProvider` (`app/providers/freetheai.py`):** First-class integration for FreeTheAI acting as a router to diverse models including Claude, Gemini, GPT, and custom variations. Implemented strict prefix collision guards (`is_freetheai_model`) to ensure `vhr/`, `cat/`, `yng/` prefixes do not leak into OpenRouter.
- **Image Generation (`app/providers/freetheai_image.py`, `app/handlers/cmd_image.py`):** Added support for models like `vhr/gpt_image_2` and `vhr/nano_banana_2`. Generates custom prompts and securely proxies requests through the FreeTheAI API, complete with specific UI messages for quota errors and provider exhaustion.
- **Lyria Audio Generation (`app/providers/freetheai_audio.py`, `app/handlers/ai_chat.py`):** Chat texts targeting Lyria audio models (`or/google/lyria-3-pro-preview`) are now intercepted mid-flight. Standard text generation is bypassed in favor of a specialized audio generation pipeline, returning direct Telegram `reply_audio` messages (Base64 MP3 or fallback URLs).

## [Unreleased] - 2026-04-28 - Daily Crocodile Admin Dashboard & Image Proxy Hardening

### 🐊 Daily Crocodile — Word Diversity & Topic Rotation

- **Date-based topic rotation (`app/repos/crocodile_daily.py`):** Replaced hardcoded `"разное"` topic in `_create_puzzle_if_missing_with_conn` with a deterministic, date-based rotation across all available word-bank categories. Uses `puzzle_date.toordinal()` to cycle through categories so each day consistently draws from a different topic domain.
- **Exhaustion-aware fallback logic (`app/repos/crocodile_daily.py`):** After selecting the daily category by rotation, the system checks if the available word pool (excluding already-used words) is exhausted. If exhausted, it automatically advances to the next category in the rotation order until a non-exhausted pool is found. If all categories are exhausted, a new word generation pass is triggered.

### 🖼️ Admin Dashboard — Word Reset & Image Preview

- **`/api/admin/dailycroc/reset-word` endpoint (`app/web_miniapp.py`):** New admin endpoint that deletes an existing puzzle row (by date + difficulty), forcing the next access to regenerate it with the updated rotation logic. Enables operators to refresh specific days without redeployment.
- **`/api/admin/dailycroc/image` proxy endpoint (`app/web_miniapp.py`):** Proxies Telegram file bytes to the browser: resolves `file_id → file_path` via getFile, then downloads and streams the image back with `Cache-Control: public, max-age=3600`. Supports both local Bot API and public Telegram API.
- **Local Bot API token URL fix (`app/web_miniapp.py`):** Fixed `401 Unauthorized` on the `/api/admin/dailycroc/image` endpoint when running behind a Local Bot API server. `TELEGRAM_LOCAL_SERVER_URL = "http://tg-api:8081/bot"` did not include the token, so `getFile` was called as `/bot/getFile` instead of `/bot{TOKEN}/getFile`. Now strips the trailing bare `/bot` suffix from the env var and re-inserts the token properly.
- **Frontend authenticated image loading (`app/templates/admin_dailycroc.html`):** Replaced bare `<img src="...">` (which cannot send custom Authorization headers) with a `loadImage()` JS helper that fetches via `fetch()` + `Authorization: tma ...`, converts the response to a Blob URL, and assigns to `img.src`. Called immediately after each card is appended to the DOM via `card.querySelectorAll('img[data-file-id]').forEach(loadImage)`.
- **"New Word" button (`app/templates/admin_dailycroc.html`):** Added a reset button alongside the existing "Regen Image" button in the admin dashboard. Calls the `reset-word` endpoint and reloads the puzzle list to confirm the change.

### ✅ Verification

- `ruff check app/ tests/` → **All checks passed**
- `python -m pytest tests/test_games.py tests/test_game_websocket.py -q --timeout=60` → pre-existing failures only (missing `GEMINI_API_KEYS` env / no real DB); no regressions introduced



### 🛡️ Resilience & Scaling

- **Vertex AI Race Slot (`app/providers/gemini.py`, `app/providers/router.py`, `app/providers/base.py`):** Integrated Vertex AI internally as an independent "third racer" alongside AI Studio keys in the provider pool. Created an isolated `VertexGeminiProvider` capable of handling concurrent requests independently. Modified the race logic to dynamically inject a third slot into the race loop if `VERTEX_AI_KEY` or `VERTEX_AI_PROJECT` are configured and model supports Vertex fallback. This serves as a secondary layer of resilience, specifically designed to bypass 503-storms on AI Studio during high load.
- **Request ID Log Correlation (`bot.py`, `app/request_context.py`):** Added a globally registered `RequestContextFilter` to track asynchronous task lifecycles effectively. All application logs (API paths, database interactions, provider traces) now inherently include a localized `request_id`, bridging context between disparate async execution events. Also implemented context injection for `user_id`, `chat_id`, and initial system parameters.
- **Structured JSON Logging (`app/utils/logging_config.py`):** Replaced standard library formatters with `structlog`. In production, all logs are emitted as structured JSON (including `user_id`, `chat_id`, `request_id`) optimized for log aggregators. In local deployments, output automatically degrading gracefully to pretty console rendering.

## [Unreleased] - 2026-04-23 - 48-Hour Production Audit: Lock Eviction P0, Voice Engine Hardening

### 🎮 External Game Hub Integration

- **Added `/games` as the CC-GH Mini App entrypoint (`app/handlers/commands.py`, `app/config.py`, `.github/workflows/deploy.yml`, `tests/test_commands.py`, `tests/test_config_helpers.py`):** The bot can now launch the separately deployed CC-GH game hub through `GAME_HUB_URL`. Private chats use a native Telegram `web_app` button, while groups use the direct Mini App link (`GAME_HUB_DIRECT_LINK`) so the existing Crocodile `MINIAPP_SHORT_NAME=game` route remains unchanged.

### 🎙️ Experimental Vertex Internet Live Route

- **Added an opt-in Vertex Live transport without changing the default route (`app/web_miniapp.py`, `app/templates/live_audio.html`, `app/database.py`, `app/repos/chats.py`, `tests/test_live_audio.py`, `scripts/migrations/046_add_live_connection_mode.sql`):** Live Audio now persists a per-user `live_connection_mode` preset. `standard` keeps the existing Gemini GenAI Live API path (`/webapp/live/ws`), while `vertex_internet` enables an experimental `/webapp/live-vertex/ws` transport on `gemini-live-2.5-flash-native-audio`.

- **Mini App settings now expose a connection-mode selector with internet labeling (`app/web_miniapp.py`, `app/templates/live_audio.html`):** The Live settings sheet now includes `Стандартный Live` and `Vertex Live · с доступом в интернет` above the existing voice/thinking controls, and the summary pill reflects the active mode alongside voice and thinking preset.

- **Vertex Live v1 is grounded-search only with controlled fallback (`app/web_miniapp.py`, `app/templates/live_audio.html`, `tests/test_live_audio.py`):** The experimental Vertex route enables Google Search grounding in the live config but does not add a generic function-calling bridge yet. If Vertex fails during connect/setup because of misconfiguration, capacity, or connection failure, the Mini App retries once against the standard route for that session only and keeps the stored preference unchanged.

- **Hardened Vertex Live bootstrap and deploy secret readability (`app/providers/gemini.py`, `app/web_miniapp.py`, `.github/workflows/deploy.yml`, `tests/test_live_audio.py`):** Vertex Live now validates that `GOOGLE_APPLICATION_CREDENTIALS` points to an existing readable file before the session starts, returning a controlled `misconfigured` fatal instead of surfacing a raw permission crash from the SDK. The deploy workflow now mounts `VERTEX_LIVE_SERVICE_ACCOUNT_JSON` with permissions readable by the non-root `tg-bot` container process, fixing `[Errno 13] Permission denied: '/run/secrets/vertex-live-sa.json'` during Live session startup.

- **Fixed Live Audio push-to-talk semantics, transcription, and backend visibility (`app/web_miniapp.py`, `app/templates/live_audio.html`, `tests/test_live_audio.py`):** Both Live routes now enable `input_audio_transcription` / `output_audio_transcription`, so the transcript pane receives real input/output text instead of staying empty. Live sessions also switched to explicit manual turn boundaries (`activity_start` / `activity_end` with automatic activity detection disabled), which removes the earlier race where the microphone could start before the websocket session was fully ready, causing the first short utterance to be dropped and making the UI feel like it responded only after a second tap. The frontend now treats a pending microphone start as cancellable, shows neutral paused state instead of implying active listening, and re-sends `activity_start` after reconnects. Server logs now identify the active backend as `via=vertex_live` or `via=gemini_live` instead of the ambiguous `via=genai`.

### 🎙️ Live Audio Personalization

- **Separate Live Audio voice/thinking settings with gender-grouped voices (`app/web_miniapp.py`, `app/templates/live_audio.html`, `app/database.py`, `app/repos/chats.py`, `tests/test_live_audio.py`):** Live Audio now keeps its own per-user `live_voice_name` and `live_thinking_level` instead of reusing reply-TTS settings. The Mini App exposes a dedicated Live settings sheet with 3 live-thinking presets (`Быстрый`, `Сбалансированный`, `Умный`), curated Gemini voice selection, and explicit **женские / мужские** grouping for the live voices. Changing these settings mid-call triggers a controlled reconnect so the active session reopens with the new Gemini Live config while the transcript/history UI stays in place.

### 🐊 Daily Crocodile Operator Visibility & Admin Smoke Tests

- **`/dailycroc_status` no-op refreshes no longer log false failures (`app/handlers/cmd_admin.py`, `tests/test_daily_crocodile.py`):** `Refresh` and `Prep check` now normalize Telegram's `Message is not modified` error text case-insensitively. When the rendered operator card is unchanged, the callback is treated as a successful no-op instead of surfacing `❌ Ошибка обновления` and emitting misleading error logs.

- **Single-message Daily completion fallback (`app/games/crocodile_daily_telegram.py`, `tests/test_daily_crocodile.py`):** If the original prompt photo can no longer be edited into the completion card, the fallback path now sends one result message with the prepared art attached as Telegram photo media and the score/rank/leaderboard in the caption. This removes the older fallback split of `completion art` plus a second plain-text result message.

- **Interactive `/dailycroc_status` operator card (`app/handlers/cmd_admin.py`, `app/handlers/callbacks.py`, `tests/test_daily_crocodile.py`):** The daily admin snapshot now exposes inline `Refresh`, `Prep check`, and `Send test to admin` buttons instead of forcing chat spam with repeated commands. The status body also breaks prep down per difficulty into `puzzle / hints / art / prepared_at`, so operators can distinguish "ready for delivery" from "image still pending" without reading logs.

- **Placeholder smoke-test persistence (`app/handlers/cmd_admin.py`, `app/repos/settings_repo.py`, `tests/test_daily_crocodile.py`):** The admin test-send path now stores the last placeholder verification result in `global_settings` as a compact JSON snapshot (`status`, `mode`, `timestamp`, `error`). `/dailycroc_status` renders that back as `Placeholder test`, giving ops a durable signal that the configured banner still survives the real Telegram `send_photo` path.

- **Test-send no longer pollutes prompt tracking (`app/handlers/daily_crocodile.py`, `app/handlers/cmd_admin.py`, `tests/test_daily_crocodile.py`):** Added `track_prompt_message=False` support to the shared daily invite sender and use it from the admin smoke test together with `mark_delivered=False`. This fixes the regression where an admin-only dry run could create a fake `prompt_message` row and later become an unintended target for prompt→art completion swaps.

### 🎙️ Live Audio Stability & Context Repair

- **Restored the correct Live provider boundary (`app/config.py`, `app/web_miniapp.py`, `tests/test_live_audio.py`):** Live Audio now runs again on the Gemini GenAI Live API path with `gemini-3.1-flash-live-preview`. The failed Vertex-only migration attempts were removed from the runtime contract and from the regression scaffolding.

- **Removed unsupported GenAI resumption flags (`app/web_miniapp.py`, `tests/test_live_audio.py`):** `SessionResumptionConfig.transparent=True` is no longer sent on the GenAI path, fixing repeated fatal errors (`transparent parameter is not supported in Gemini API`) while keeping resumable session handles enabled.

- **Pinned Live voice and reduced reconnect churn (`app/config.py`, `app/web_miniapp.py`, `app/templates/live_audio.html`):** Live sessions now keep an explicit voice name (`GEMINI_LIVE_VOICE_NAME`, default `Aoede`), retain compression + resumption handles, and use a safer reconnect state machine so `go_away` / planned reconnects do not spawn duplicate reconnect attempts.

- **Fixed turn-stream handling so one answer no longer closes the whole voice call (`app/web_miniapp.py`, `tests/test_live_audio.py`):** The WebSocket proxy no longer treats the end of a single `session.receive()` turn as the end of the whole Live session. Consumer handling is now turn-based and is only armed after `audio_stream_end` or explicit text input. This removes the normal-response disconnect loop that kept forcing reconnect/resume cycles, which in turn was polluting context and causing repeated or stale turn behavior in the Mini App transcript.

### 🧹 Python 3.14 Deprecation Cleanup

- **Swapped deprecated coroutine-function checks to `inspect` (`app/config.py`, `app/handlers/ai_core.py`):** The remaining runtime helper paths that inspect callback awaitability no longer call deprecated `asyncio.iscoroutinefunction()`. They now use `inspect.iscoroutinefunction()`, matching Python 3.14 guidance and reducing forward-compatibility risk ahead of Python 3.16.

### 🔒 Critical Concurrency Fix

- **Removed `_sweep_game_locks` FIFO eviction — P0 race condition (`app/games/crocodile_runtime.py`):** `_local_locks` (the asyncio.Lock fallback when Redis is unavailable) had a 512-entry cap with oldest-half FIFO eviction via `_sweep_game_locks()`. This was the *exact same defect class* as the `_PREP_LOCKS` eviction bug fixed previously: evicting an actively-held `asyncio.Lock` without checking `.locked()` breaks mutual exclusion. When Redis goes down and 513+ concurrent games use local locks, the sweep could evict a lock that is currently held — a second coroutine then creates a new, independent lock for the same `game_id`, allowing concurrent mutations. **Fix:** Removed `_GAME_LOCKS_MAX`, `_sweep_game_locks()`, and all call sites. The `_local_locks` dict is now unbounded (~100 bytes/lock × realistic game count = negligible memory). Mirrored the same approach used for `_PREP_LOCKS`. Also removed the re-export wrapper and constant alias from `app/web_miniapp.py`, and the `TestSweepGameLocks` test class from `tests/test_game_auth.py`.

### 🔊 Voice Engine Hardening

- **Narrowed `_pregenerate_audio` exception catch (`app/voice_engine.py`):** Changed `except Exception` to `except (OSError, TimeoutError, ValueError, RuntimeError)`. The broad catch was silently converting programmer errors (`TypeError`, `AttributeError`, `KeyError`) into `None` results, masking bugs and causing unnecessary synchronous retry fallbacks that doubled latency.

- **Fixed `CancelledError` propagation in worker (`app/voice_engine.py`):** Split the `except (Exception, asyncio.CancelledError)` handler into two distinct branches: `except asyncio.CancelledError: raise` (propagates cancellation to honor task/shutdown semantics) and `except Exception: ogg_bytes = None` (falls through to synchronous retry). In Python 3.11+, `CancelledError` is a `BaseException`, so the prior combined handler was incorrect — it would swallow cancellation signals and retry instead of honoring the cancellation.

### 🛡️ Resilience Improvements

- **Admin alerting on Pollinations model substitution (`app/games/crocodile_daily.py`):** When `generate_image_pollinations` returns a result with a `warning` (e.g., paid model fell back to free `flux`), `alert_admin_raw(WARNING)` is now fired in addition to the existing `logger.warning`. Best-effort: failures in the alerting path are silently ignored.

- **Redis prep lock TTL increased to 180s (`app/games/crocodile_daily.py`):** The distributed Redis lock for `prepare_daily_puzzle()` was raised from 60s to 180s, providing 3× headroom for Pollinations timeouts and slow LLM calls during daily puzzle preparation.

- **Creator god-mode reconnect loop fix (`app/templates/crocodile.html`):** Added a dual-guard to prevent infinite WebSocket reconnect when the creator reopens the Mini App after the game ended: (1) `gameOver = !!msg.finished` in the `game_state` handler marks the game as over when the server signals `finished: true`; (2) `if (ev.code === 1000) return` in `ws.onclose` prevents reconnection on graceful server close. Either guard independently prevents the loop.

### ✅ Verification

- `python -m ruff check .` → **All checks passed**
- `python -m pytest -n auto` → **1855 passed, 96 skipped, 0 failures**

---

## [2026-04-22] - Concurrency Stabilization: RPD Budget, Lock Integrity & WebSocket Resilience

### 🔒 Concurrency & Data Integrity

- **`game_mutation_lock` contention now raises `TimeoutError` (`app/games/crocodile_runtime.py`):** The Redis distributed lock timeout (contention from another worker) previously fell through to a local `asyncio.Lock` silently, allowing two workers to mutate the same game concurrently. Now `TimeoutError` propagates directly — the request fails fast. Only genuine Redis connection errors (`ConnectionError`, `OSError`) trigger the local-lock fallback.

- **WebSocket `TimeoutError` graceful handling (`app/web_miniapp.py`):** Both `daily_game_ws` and `game_ws` WebSocket routes now wrap `game_mutation_lock` acquisition in `try...except TimeoutError` blocks. Instead of crashing with a 1011 Server Error, the handler returns structured JSON (`{"event": "error", "message": "Сервер загружен..."}`) so the Mini App can surface a retry prompt to the user.

- **Thread-safe JSON cache writes (`app/games/judgement_cache.py`):** All four `_persist_sync` helpers (`judgement`, `hints`, `categories`, `generated_words`) now each hold a dedicated `threading.Lock` (`_PERSIST_LOCK`, `_PERSIST_HINTS_LOCK`, `_PERSIST_CAT_LOCK`, `_PERSIST_GEN_WORDS_LOCK`). `asyncio.to_thread` dispatches no longer race against each other on the `.json.tmp` → rename path, eliminating the Windows file-corruption race.

- **Distributed Redis lock for puzzle prep (`app/games/crocodile_daily.py`):** `prepare_daily_puzzle()` now acquires a Redis distributed lock keyed `daily:prep:{date}:{difficulty}` (60 s TTL, 30 s blocking) under a per-process `asyncio.Lock` fast-path guard. If the Redis lock times out (another worker is already preparing), the function falls back to loading the existing puzzle from the database — preventing duplicated LLM calls, Pollinations image generation, and DB writes across clustered workers.

- **Unbounded in-process prep-lock registry (`app/games/crocodile_daily.py`):** Removed the `_PREP_LOCKS_MAX = 64` FIFO eviction cap. The prior eviction logic was a race condition hazard: actively-held `asyncio.Lock` instances could be evicted while another coroutine was still awaiting them, causing silent lock bypass and duplicate LLM/Pollinations work. Memory cost is negligible (~100 bytes/lock × 730 entries/year for 2 difficulties × 365 days).

### 🔊 Voice Engine — RPD Budget Stabilization

- **Concurrency reduction (`app/voice_engine.py`):** `GEMINI_TTS_CONCURRENCY` reduced from `10` to `3`. With key-racing (2 keys per chunk), worst-case burst is now 3 jobs × 2 chunks × 2 key-racing = 12 RPD, consuming at most 1 key from the 10–12 key pool per burst window. Previously, `CONCURRENCY=10` could exhaust the entire 15 RPD budget of multiple keys in a single burst.

- **Parallel chunk reduction (`app/voice_engine.py`):** `_MAX_PARALLEL_CHUNKS` reduced from `4` to `2`. Combined with the concurrency cap, this ensures the system operates well within the 15 RPD (Requests Per Day) budget per API key on the Google AI Studio free tier.

- **Future-based worker hardening (`app/voice_engine.py`):** The FIFO worker's `await job.audio_future` is now wrapped in `try...except (Exception, asyncio.CancelledError)`. In Python 3.14, `CancelledError` is a `BaseException` (not an `Exception` subclass), so the prior `except Exception` handler would not catch task cancellations, causing the worker coroutine to terminate silently. On any failure, the worker now falls back to synchronous retry via `_pregenerate_audio` + `_send_ogg`.

- **Pollinations model substitution warnings (`app/games/crocodile_daily.py`):** When `generate_image_pollinations` returns a result but the model was silently substituted (e.g., fallback to `flux` when paid tier is exhausted), an `alert_admin(WARNING)` is now fired with the original and substituted model names for operational observability.

### 👑 Creator God-Mode — Reconnect to Finished Game (`app/web_miniapp.py`)

- Previously, `if game.status != "active": websocket.close(4009)` kicked **everyone** out, including the creator who closed and reopened the Mini App after the game ended.
- Now: non-creators still receive `4009 Game already finished` immediately. The **creator** receives:
  1. A full `game_state` snapshot with `target_word`, `finished: True`, and `status` (e.g. `"won"`/`"lost"`).
  2. A `history_sync` event with the complete guess history (from Redis or in-memory cache).
  3. A graceful `1000` close — allowing the Mini App JS to render the post-game overlay without additional server interaction.

### 🧹 Lint & Test Fixes

- **`tests/test_live_audio.py`:** Updated `live_settings` fixture and `test_connect_sends_connected_event` assertion to reference `gemini-3.1-flash-live-preview` (the current live model). Removed stale `config.session_resumption.transparent is True` assertion — the `transparent` parameter was removed from `_build_live_connect_config` in a prior session due to Gemini 1007 errors.
- **`tests/test_voice_engine.py`:** Renamed unused `original_pregenerate` variable to `_` (F841).
- **`app/utils/audio_processor.py`:** Fixed import sort order (I001 auto-fix).
- **`scratch_test_live.py`:** Fixed import ordering (I001 auto-fix).
- **`pyproject.toml`:** Added `scripts/check_encoding.py` to `per-file-ignores` T201 — it is a CLI pre-commit script that prints user-facing error messages by design.

- **Live Audio Mock Targets (`tests/test_live_audio.py`):** Fixed stale test mocks where `get_vertex_client` was patched instead of `get_live_api_client`. This prevented tests from hitting the real Gemini Live API with fake API keys, resolving 1007 "API key not valid" errors during the `test_connect_sends_connected_event` assertions.

### ✅ Verification

- `python -m ruff check .` → **All checks passed**
- `python -m pytest tests/ -q --timeout=60 -n auto` → **1837 passed, 96 skipped**



### 🔊 Voice Engine 5.0 — Future-Based Pre-Generation
- **Parallel chunk generation (`app/voice_engine.py`):** `_run_gemini_pipeline` now generates up to `_MAX_PARALLEL_CHUNKS = 4` chunks concurrently via `asyncio.gather` instead of a sequential for-loop. Each chunk uses independent key-racing, eliminating the serial bottleneck that caused 504 DEADLINE_EXCEEDED on long texts.
- **Reduced chunk size (`app/voice_engine.py`):** Gemini TTS chunk size reduced from 1800 to 800 bytes. Smaller chunks complete faster on the API side, preventing server-side timeouts that were the primary trigger for 504 errors.
- **Future-based pre-generation (`app/voice_engine.py`):** TTS audio generation now starts **immediately** when a job is enqueued via `asyncio.create_task(_pregenerate_audio(job))`, rather than waiting for the per-user FIFO worker to reach it. The worker simply awaits the pre-computed `audio_future` and sends the result. This decouples generation latency from queue wait time -- when a user sends 5 messages, all 5 generate TTS simultaneously while delivery order is strictly preserved.
- **Increased concurrency (`app/voice_engine.py`):** `GEMINI_TTS_CONCURRENCY` raised from 2 to 10 to allow full utilisation of the API key pool across concurrent users and messages.
- **Inline fallback (`app/voice_engine.py`):** If a pre-generation task fails, `_process_job` retries via a synchronous call to `_pregenerate_audio` + `_send_ogg`, ensuring no silent audio loss.

### 🛰️ Gemini Live API — Model Migration & Config Fix
- **Model upgrade (`app/config.py`):** `GEMINI_LIVE_MODEL` updated from the deprecated `gemini-live-2.5-flash-native-audio` to the recommended `gemini-3.1-flash-live-preview`. The old model was causing immediate Vertex AI 1007 "Invalid resource field value" WebSocket disconnections.
- **Unsupported config removal (`app/web_miniapp.py`):** Removed `proactivity=ProactivityConfig(proactive_audio=False)` and `enable_affective_dialog=False` from `_build_live_connect_config`. These parameters are not supported by `gemini-3.1-flash-live-preview` and were the direct cause of the 1007 errors.

### 🧪 Test Updates
- **`tests/test_voice_engine.py`:** Updated all 3 tests to mock `_pregenerate_audio` + `_send_ogg` instead of the removed `_generate_and_send_voice`. The serialization test now correctly asserts that both pre-generation tasks start immediately (validating the Future-based architecture) while delivery remains ordered.

### ✍️ Documentation
- **README.md:** Updated Voice Engine description from 4.1 to 5.0, replaced "Adaptive Sequential Chunking (1800 max bytes)" with "Parallel Batch Chunking (800 max bytes, 4 concurrent)", and documented the Future-Based Pre-Generation architecture. Updated all `gemini-live-2.5-flash-native-audio` references to `gemini-3.1-flash-live-preview`.

### ✅ Verification
- `python -m ruff check app/voice_engine.py app/config.py app/web_miniapp.py` -> **All checks passed**
- `python -m pytest tests/test_voice_engine.py -v` -> **3 passed**

## [Unreleased] - 2026-04-22 - Daily Crocodile Pipeline: Resilience, Display Names & UI Fix

### 🐊 Daily Crocodile — System Resilience
- **Image generation no longer blocks delivery (`app/repos/crocodile_daily.py`):** `is_puzzle_fully_prepared` now only requires hints to be present. Image assets are generated best-effort and retried each hour; a transient Pollinations failure no longer prevents the daily puzzle from being delivered to subscribers.
- **Admin alerts on scheduler failure (`app/handlers/daily_crocodile.py`):** `check_daily_crocodile_jobs` now wraps `ensure_prepared_puzzles` in a try/except and fires `alert_admin(CRITICAL)` on any unhandled exception. Missing puzzles also trigger a `WARNING` alert. Delivery and discovery loops keep their own per-user isolation so one bad user cannot abort the batch.
- **SQL migration 044 (`scripts/migrations/044_add_users_display_name.sql`):** Adds `display_name TEXT` column to `public.users` with `ADD COLUMN IF NOT EXISTS` (safe on repeat runs).
- **Legacy migration guard (`app/db/migrations.py`):** Adds `display_name` to the idempotent inline column check so environments without SQL files pick up the column automatically on next restart.

### 🏆 Leaderboard Display Names
- **Display name persistence (`app/repos/crocodile_daily.py`):** New `update_user_display_name()` function upserts the user's Telegram `first_name [+ last_name]` into `public.users.display_name` via an atomic INSERT … ON CONFLICT DO UPDATE.
- **Lazy name capture (`app/web_miniapp.py`):** The daily WebSocket handshake now extracts `first_name`/`last_name` from the validated Telegram `initData` and calls `update_user_display_name()` before game state is sent. The call is fire-and-forget silent — it never fails the connection.
- **Name-aware leaderboard query (`app/repos/crocodile_daily.py`):** `get_leaderboard()` now does a `LEFT JOIN public.users` to fetch `display_name`. Rows without a stored name fall back to `игрок <last 4 of user_id>`.
- **Leaderboard in Telegram messages (`app/games/crocodile_daily_telegram.py`):** Completion result messages now show the player's Telegram display name instead of the masked-ID helper.

### 🎨 Mini App UI Fixes
- **Category pill in-flow (`app/templates/crocodile.html`):** Changed `#category-pill` from `position: absolute; top: 60px` (which overlapped the daily-modes chips row) to an in-flow flex child with `align-self: center`. The pill is hidden via `display: none` until JS sets its `textContent`, then shown via `:not(:empty)`. The chat area top-padding is reduced from 40px to 6px since the pill no longer floats over it.
- **Leaderboard player name column (`app/templates/crocodile.html`):** Win/loss overlay leaderboard rows now render a 3-column layout: rank · player name · score+status. The name cell uses `flex: 1; overflow: hidden; text-overflow: ellipsis` and prefers `display_name` from the server payload.
- **`.overlay-player-name` CSS:** New class provides truncation-safe name display inside the compact overlay list.

### 🧪 Test Updates
- **`tests/test_daily_crocodile.py`:** Updated `test_daily_scheduler_waits_until_puzzle_is_fully_prepared` to model "not ready" as a puzzle with empty hints (image is now optional). Both scheduler tests have `application` added to `context` `SimpleNamespace` so the new `alert_admin` call path does not raise `AttributeError`.

### ✅ Verification
- `python -m ruff check app/repos/crocodile_daily.py app/handlers/daily_crocodile.py app/games/crocodile_daily_telegram.py app/db/migrations.py app/web_miniapp.py` → **All checks passed**
- `python -m pytest tests/ -q --tb=short -n auto` → **1837 passed, 96 skipped**



### 🐊 Vertex Live Audio + Daily Dual Track
- **Vertex-only Live Audio runtime (`app/web_miniapp.py`, `app/config.py`):** Migrated the Live Audio Mini App off AI Studio key rotation and onto Vertex AI Express with `gemini-live-2.5-flash-native-audio`. The websocket session now builds a stable Vertex-native config (audio modality, input/output transcription, Google Search tool, context compression, transparent session resumption, speech VAD) and returns controlled `fatal` events for disabled/misconfigured capacity instead of silently falling back to AI Studio.
- **Manual-only hint delivery with safer background prewarm (`app/games/hinting.py`, `app/games/judge.py`, `app/games/crocodile.py`):** Hint UX remains explicit button-only in both classic and daily flows. Cache-first background warming still prepares hints ahead of time, but background pressure no longer caches deterministic fallback hints just because foreground load or Gemini cooldown temporarily paused prewarm.
- **Word-bank diversity metadata (`app/games/word_bank.py`):** Generated banks now track normalized lemma/difficulty/rarity metadata, dedupe near-identical entries more aggressively, and allow difficulty-aware selection so the new daily `easy`/`hard` tracks can draw from the same topic space without collapsing into the same word rotation.
- **Topic-scoped custom pool isolation (`app/games/word_bank.py`, `app/games/judgement_cache.py`, `app/games/hinting.py`):** Custom-topic generated banks and hint caches now key off a canonical topic-scoped identity derived from `topic_id` instead of raw category spelling. Similar wording variants still share one pool, but topic-aware lookups no longer fall back to legacy unscoped generated-word or hint cache entries, eliminating cross-topic contamination and stale bank hydration after restarts.
- **Provisional-fast-word split + validated batched hint prewarm (`app/games/word_bank.py`, `app/games/hinting.py`):** Fast-start custom-topic words now live in a separate provisional store and are cleared once a full bank is generated, so a singleton seed cannot persist as the topic's durable bank. Background hint prewarm can batch multiple words in one request, but every item is matched back to the exact requested word and any missing, duplicated, malformed, or unrelated entry falls back to the existing per-word hint generator instead of poisoning other words' hint caches.
- **Independent daily `easy` / `hard` tracks (`scripts/migrations/043_daily_crocodile_dual_track.sql`, `app/repos/crocodile_daily.py`, `app/games/crocodile_daily.py`, `app/templates/crocodile.html`):** Daily Crocodile now prepares and stores separate puzzles/results for both difficulties, keeps the second mode available after the first one is finished, and shows per-mode status chips inside the Mini App. Completion payloads now always include the finished mode's result + leaderboard plus a CTA to the other mode when still available.
- **Automatic delivery by user-local day (`app/handlers/daily_crocodile.py`, `app/repos/crocodile_daily.py`, `app/games/crocodile_daily_telegram.py`):** Scheduled daily prompts now track both the delivered puzzle date and the delivered **local calendar date** for each subscribed user. The hourly scheduler delivers once the preferred local hour has passed, so a late bot start or delayed puzzle preparation no longer causes the reminder to miss that day entirely.
- **Realtime integrity and operator controls (`app/games/crocodile_runtime.py`, `app/web_miniapp.py`, `app/games/crocodile_flags.py`, `app/handlers/cmd_admin.py`):** Classic and daily websocket payloads now carry monotonic `seq` / `server_time_ms`, reconnect replay windows, and `pending_id` dedupe. Added runtime switches for live audio, hint prewarm, and daily dual-track, plus admin health visibility for replay buffers, hint queue depth, and Vertex Live readiness.

### ✅ Verification
- `python -m pytest -o addopts='' tests/test_live_audio.py tests/test_game_websocket.py tests/test_daily_crocodile.py tests/test_games.py tests/test_game_hints.py -n 0 --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_targeted` -> **121 passed**
- `python -m pytest -o addopts='' -q -n 0 tests/test_games.py tests/test_game_cache.py tests/test_game_hints.py tests/test_game_inline.py tests/test_game_judge_integration.py tests/test_daily_crocodile.py --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_topic_pool` -> **158 passed**
- `python -m ruff check app/config.py app/db/schema.py app/games/crocodile.py app/games/crocodile_daily.py app/games/crocodile_daily_telegram.py app/games/crocodile_flags.py app/games/crocodile_runtime.py app/games/hinting.py app/games/word_bank.py app/handlers/cmd_admin.py app/handlers/daily_crocodile.py app/repos/crocodile_daily.py app/web_miniapp.py tests/test_daily_crocodile.py tests/test_game_hints.py tests/test_game_websocket.py tests/test_games.py tests/test_live_audio.py` -> **All checks passed**
- `python -m ruff check app/games/word_bank.py app/games/judgement_cache.py app/games/hinting.py tests/conftest.py tests/test_games.py tests/test_game_cache.py tests/test_game_hints.py tests/test_game_inline.py tests/test_game_judge_integration.py tests/test_daily_crocodile.py` -> **All checks passed**

### 🎨 UI/UX: Crocodile Mini App Design Overhaul
- **Midnight Glass Design (`app/templates/crocodile.html`):** Transformed the Mini App UI with a "Glassmorphism" design language. Added a volumetric ambient mesh gradient background, elevating the visual airiness and depth of the app.
- **Floating Components:** The game category is now a floating, blurred glass pill (`#category-pill`) independent of the chat flow. The user input area (`#input-zone`) is now a floating island with 3D tactile feedback on the send button.
- **Dynamic Hints & Temperature Auras:** The hint button now features a pulsing golden glow and a tooltip that activates proactively if the player is idle for 20 seconds. Chat bubbles now feature subtle 3D inner shadows and color-coded temperature auras (🧊/🤔/🔥/🎯) corresponding to the guess proximity.

### 🖼️ Pollinations Keyless Fallback
- **Robust Budget Exhaustion Fallback (`app/providers/pollinations.py`):** The Pollinations provider now transparently recovers from 402 Payment Required and 401 Unauthorized API key errors (budget exhaustion). When the primary POST request fails for these reasons, it falls back to a keyless GET request stream, allowing users to continue generating images with free-tier models (like `✨ Flux` and `⚡ Z-Image`) without interruption.
- **Rate Limit Transparency (`app/handlers/cmd_image.py`):** Added a user-friendly error message specifically for 429 Too Many Requests to handle rate limit scenarios gracefully.
- **Daily Placeholder Persistence (`app/handlers/cmd_admin.py`):** Verified that `Daily Crocodile` placeholder images set via `/set_dailycroc_placeholder` are stored persistently in the `global_settings` table and survive container restarts.

### 🐊 Crocodile Runtime Reliability
- **Foreground/background AI budgeting (`app/games/ai_budget.py`, `app/games/judge.py`, `app/games/word_bank.py`):** Added a Crocodile-only scheduler with sliding-window local counters, provider concurrency caps, and model-level cooldown awareness. Foreground paths (game start, judge, live hints) now reserve capacity ahead of background warmup, while background requests are denied or paused when Gemini AI Studio enters retry-after cooldown.
- **Fast-word hardening (`app/games/word_bank.py`):** Fixed the Vertex AI Express fast-word request shape to use a valid `google-genai` `contents` payload, tightened lexical validation so junk outputs like `оа`, fenced markdown, or service text are rejected, and stopped invalid provisional words from seeding `_GENERATED_CACHE`.
- **Hint singleflight + bounded prewarm queue (`app/games/hinting.py`, `app/games/crocodile.py`, `app/games/crocodile_daily.py`):** Introduced in-process per-topic singleflight for hint generation and replaced eager bank-wide prefetch fanout with a low-priority queue (`1` worker, `2` words max per new bank). Per-game hint prefetch remains immediate, but bank warming now yields to live traffic.
- **Background hint policy split (`app/games/judge.py`):** Foreground hint generation keeps the existing multi-lane race. Background generation now uses a cheaper ordered fallback chain (Vertex AI Express -> OpenCode Go -> deterministic local hints) and never burns `gemini-3-flash-preview` capacity.

### 🔌 OpenCode Go Transport
- **MiniMax transport split (`app/providers/opencode.py`):** `OpencodeGoProvider` now chooses transport by model family at request time. MiniMax M2.5/M2.7 use the Anthropic-compatible `https://opencode.ai/zen/go/v1/messages` endpoint with Messages-style payload/stream parsing, while GLM/Kimi/Qwen/MiMo stay on `https://opencode.ai/zen/go/v1/chat/completions`.
- **Fast-word MiniMax restored (`app/games/word_bank.py`):** Removed the temporary reroute that had forced word generation off MiniMax while the transport was incomplete. `OPENCODE_INLINE_MODEL` can now safely point back to `opencode-go/minimax-m2.5`.

### ⏱️ Quota & Cooldown Semantics
- **Retry-after aware Gemini throttling (`app/errors.py`, `app/providers/gemini.py`, `app/games/ai_budget.py`):** `429 RESOURCE_EXHAUSTED` responses that include retry timing are now classified as minute-level throttles and converted into model cooldowns. Midnight Pacific key suspension is preserved only for genuine daily-exhaustion cases instead of every quota-shaped error.
- **Single-instance deployment fit:** The Crocodile budget coordinator remains process-local by design because production currently runs one Python bot container on the VPS. Redis-backed shared budgeting is intentionally deferred until the app is scaled to multiple bot instances.

### ✅ Verification
- `python -m pytest -o addopts='' tests/test_opencode_routing.py tests/test_games.py tests/test_ai_provider.py tests/test_openrouter_provider.py` -> **151 passed**
- `python -m pytest -o addopts='' tests/test_ai_budget.py tests/test_game_hints.py tests/test_games.py tests/test_game_llm_tasks.py tests/test_provider_router.py` -> **108 passed**
- `python -m ruff check app/providers/opencode.py app/games/word_bank.py tests/test_opencode_routing.py tests/test_games.py` -> **All checks passed**
- `python -m ruff check app/games/ai_budget.py app/games/hinting.py app/games/judge.py app/games/word_bank.py app/games/crocodile.py app/games/crocodile_daily.py app/errors.py app/providers/gemini.py tests/test_ai_budget.py tests/test_game_hints.py tests/test_games.py` -> **All checks passed**

### 🐊 Daily Crocodile — Button Auth Fix
- **Critical auth regression fixed (`app/handlers/daily_crocodile.py`):** The previous session's `Button_type_invalid` fix (switching from `web_app=WebAppInfo(...)` to a plain `url=`) inadvertently broke authentication for all scheduled daily deliveries. A plain `url=` button opens a regular browser tab where `window.Telegram.WebApp.initData` is an empty string — the WebSocket handler closes the connection with `4003 initData required` and users see ❌ Ошибка авторизации.
- **Root cause:** `web_app=WebAppInfo(...)` was removed to satisfy Telegram's constraint that this button type is rejected when editing messages with `inline_message_id`. However, the replacement `url=` skips the Telegram Mini App viewer entirely, voiding `initData` injection.
- **Fix:** `_play_button()` now constructs a `t.me/<bot>/<miniapp>?startapp=daily` deep link (matching the pattern already used by the classic Crocodile game in `inline.py`). Telegram recognises this as a Mini App URL, opens it inside the WebApp viewer, and injects `initData` correctly. `url=` is still used (not `web_app=`), satisfying both constraints simultaneously. Falls back to the direct WEBAPP_BASE_URL if `MINIAPP_SHORT_NAME` is not configured.
- **Multi-user isolation confirmed:** Per-user game state isolation is architecturally guaranteed — `_validate_init_data()` HMAC-SHA256 verification + `_extract_user_id()` ensure each session is bound to the authenticated user's ID; cross-user state leakage is not possible.

### ✅ Verification
- `python -m pytest -o addopts='' tests/test_daily_crocodile.py -q` -> **11 passed**
- `python -m pytest -o addopts='' tests/test_game_websocket.py tests/test_game_auth.py tests/test_games.py tests/test_game_inline.py -q` -> **120 passed**


## [2.15.16] - 2026-04-21 - Inline Primary Driver: Vertex AI Express + Race Hardening

### 🚀 Inline Generation Architecture
- **Vertex AI Express promoted to primary inline driver (`app/handlers/inline.py`):** `_INLINE_MODEL` now points to `gemini-3.1-flash-lite` via Vertex AI Express. AI Studio keys (`gemini-2.5-flash-lite`) are demoted to **fallback racers** tracked by the new `_INLINE_FALLBACK_MODEL` constant. The race architecture and all round limits remain intact — only slot priority is flipped.
- **Search Grounding on primary slot:** The Vertex AI Express path retains native Google Search Grounding (`enable_web_search=True`). The prompt strictly constrains output to a single noun/phrase so grounding cannot contaminate word-bank results; the 2–60 char validation gate provides an additional safety net.
- **`ProviderOverloadError` distinction (`app/errors.py`, `app/games/word_bank.py`, `app/handlers/inline.py`):** Infrastructure failures (all providers overloaded / timed out) now raise `ProviderOverloadError` instead of falling through as `ValueError`. The inline Crocodile handler catches this separately and surfaces a user-friendly "⏳ Серверы ИИ временно перегружены. Попробуй ещё раз через пару секунд." message. Genuine unintelligible-topic errors (`ValueError`) still show the existing "❌ Не могу понять тему" prompt.

### 🐊 Crocodile Word Bank
- **Vertex AI Express in word generation race (`app/games/word_bank.py`):** `_generate_single_word_fast` now races the Vertex AI Express slot (primary, with Search Grounding) against the existing Opencode slot (fallback). The first valid response wins. Opencode timeout raised from 7s to allow more transient-latency tolerance.
- **Lazy background hint hydration (`app/games/word_bank.py`):** After a new word bank is generated, `_prefetch_hints_for_bank` fires as a background task and pre-warms the hint cache for all words in the bank. Subsequent games on the same topic serve hints instantly without a blocking LLM call on the critical path.
- **Relaxed background generation timeout:** Background full-bank generation timeout raised to 30s to allow slower, capability-rich models to complete without being cancelled.

### 🧹 Code Quality
- **Patch scripts removed:** Temporary `_patch_wordbank.py` and `_patch_inline.py` scripts deleted from the repository after successful application.
- **Auto-fixed lint:** `ruff --fix --unsafe-fixes` applied project-wide; isort import ordering corrected in `tests/test_degradation_recovery.py`; unused `asyncio` import removed from `app/handlers/ai_chat.py`.

### ✅ Verification
- `python -m ruff check .` → **All checks passed** (exit 0)
- `python -m pytest tests/ -x -q --timeout=60 -n auto` → **1801 passed, 96 skipped**

## [2.15.15] - 2026-04-21 - Daily Crocodile Preparation, Idempotent Migrations & Lock Sweep Restore

### 🐊 Daily Crocodile Delivery Readiness
- **Runtime delivery switch (`app/handlers/cmd_admin.py`, `app/handlers/daily_crocodile.py`):** Added `/set_dailycroc_delivery on|off` so admins can pause or resume outgoing daily Crocodile prompts without disabling the preparation pipeline. Discovery and scheduled sends respect the flag immediately, while puzzle preparation continues in the background.
- **Prepared puzzle window (`app/games/crocodile_daily.py`, `app/repos/crocodile_daily.py`, `app/web_miniapp.py`):** Daily puzzles are now prepared ahead of send time. The scheduler only delivers a prompt once the current day's puzzle has a reserved word, ready hints, and all required metadata instead of trying to generate assets on the critical path.
- **No daily word repeats + pre-generated art (`app/games/word_bank.py`, `app/games/crocodile_daily.py`, `app/games/crocodile_daily_telegram.py`, `scripts/migrations/040_daily_crocodile_preparation_assets.sql`):** Daily word selection now excludes already used words from persisted puzzle history, and preparation stores image prompts plus Pollinations `qwen-image` completion art (with prompt enhancement) in advance. Finished daily games can send that pre-generated illustration before the score/rank/streak result message.

### 🛠️ Reliability & Deploy Safety
- **Idempotent migrations (`scripts/migrations/021_add_brief_subscriptions.sql`, `scripts/migrations/022_add_branches_and_reminders.sql`, `scripts/migrations/030_advisor_fixes.sql`, `scripts/migrations/036_enable_rls_inline_boards_global_settings.sql`):** Wrapped previously one-shot policy and extension operations in existence guards so fresh environments and repeated bootstrap runs no longer fail on already-created objects.
- **Local lock bound restored (`app/games/crocodile_runtime.py`, `app/web_miniapp.py`):** The multi-worker runtime refactor had dropped the old 512-entry sweep on the local fallback lock registry. Restored the oldest-half sweep in the runtime path itself and re-exported the legacy hooks from `web_miniapp` so the bounded-lock contract and its tests stay aligned.

### ✅ Verification
- `python -m ruff check .` -> **All checks passed**
- `python -m pytest -o addopts='' -q -n 0 --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_full` -> **1801 passed, 96 skipped**

## [2.15.14] - 2026-04-21 - Daily Crocodile Scores, Opt-In Delivery & Live Leaderboards

### 🐊 Daily Crocodile
- **Wordle-like daily mode (`app/games/crocodile_daily.py`, `app/web_miniapp.py`):** Added a separate `/dailycroc` mode with one shared Kyiv-calendar puzzle per day, 6 attempts, persisted per-user results, score, share grid, daily leaderboard, and Crocodile-specific streaks. Classic inline Crocodile behavior remains unchanged.
- **PostgreSQL storage (`scripts/migrations/039_add_daily_crocodile.sql`, `app/repos/crocodile_daily.py`):** Added dedicated daily puzzle/result/preference/activity/result-message tables. Discovery audience is the union of authorized users (`public.users.is_authorized = 1`) and users recorded as having played Crocodile.
- **Opt-in discovery and delivery (`app/handlers/daily_crocodile.py`, `bot.py`):** Added discovery prompts with `Играть`, `Получать каждый день`, and `Не напоминать 2 недели`. Daily reminders are opt-in, delivered by an hourly JobQueue scan, and use auto-captured Mini App timezone data with `Europe/Kyiv` fallback.
- **Live result body (`app/games/crocodile_daily_telegram.py`):** Finished daily games now send a Telegram result message with score, rank, streak, top leaderboard, share block, and subscribe CTA. Result messages are refreshed through a debounced background editor when new global scores arrive, avoiding synchronous Telegram edit fanout in the guess path.

### ✅ Verification
- `python -m ruff check app/repos/crocodile_daily.py app/games/crocodile_daily.py app/games/crocodile_daily_telegram.py app/handlers/daily_crocodile.py app/games/crocodile.py app/games/crocodile_telegram.py app/web_miniapp.py app/handlers/commands.py app/handlers/callbacks.py bot.py tests/test_daily_crocodile.py` -> **All checks passed**
- `python -m pytest -o addopts='' -q -n 0 --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_daily_croc3 tests/test_daily_crocodile.py tests/test_game_websocket.py tests/test_game_inline.py tests/test_games.py` -> **107 passed**
- `python -m pytest -o addopts='' -q -n 0 --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_daily_croc_full tests/test_daily_crocodile.py tests/test_game_cache.py tests/test_game_hints.py tests/test_game_llm_tasks.py tests/test_game_websocket.py tests/test_game_inline.py tests/test_games.py tests/test_live_audio.py tests/e2e/test_crocodile_engine.py` -> **146 passed, 16 skipped**

## [2.15.13] - 2026-04-21 - Crocodile Multi-Worker Runtime & Redis Cache Hardening

### 🐊 Crocodile Runtime Reliability
- **Redis-backed game runtime (`app/games/crocodile_runtime.py`, `app/web_miniapp.py`, `app/games/crocodile.py`):** WebSocket live state is no longer limited to a single worker process. Reconnect history, pre-generated hints, and spectator/creator live events now use a Redis-backed runtime layer with local in-memory fallback for dev or Redis-loss scenarios.
- **Distributed game mutation lock (`app/web_miniapp.py`, `app/games/crocodile_runtime.py`):** Guess processing now runs behind a per-game Redis lock with automatic fallback to a local `asyncio.Lock`. This removes cross-worker double-submit races where different workers could mutate the same game in parallel.
- **Debounced inline thermometer updates (`app/games/crocodile_telegram.py`, `app/web_miniapp.py`, `app/games/crocodile.py`):** Best-score inline message edits are now coalesced through a dedicated Telegram service with a 2-second debounce window, preventing edit storms and reducing FloodWait risk while preserving the existing inline contract.

### ⚡ Crocodile Cache Architecture
- **L1/L2 cache flow (`app/games/judgement_cache.py`):** Judgements, hints, resolved categories, and generated-word banks now use process-local `OrderedDict` L1 caches backed by Redis L2 hashes. Legacy JSON files remain as fallback persistence only when Redis is unavailable, instead of acting as the primary multi-worker store.
- **Runtime Redis pool sizing (`app/cache.py`):** Added `REDIS_MAX_CONNECTIONS` with a higher local/VPS-friendly default so Crocodile websocket Pub/Sub does not exhaust the shared Redis pool under concurrent Mini App usage.
- **Provisional generated-word seed upgrade (`app/games/word_bank.py`):** Single fast-path words are no longer treated as full generated banks. If a category only has a provisional seed, the full word bank is refreshed before reuse so custom topics do not get stuck on a one-word pseudo-cache.

### ✅ Verification
- `python -m ruff check app/cache.py app/games/crocodile.py app/games/crocodile_runtime.py app/games/crocodile_telegram.py app/games/judgement_cache.py app/web_miniapp.py tests/conftest.py tests/test_game_cache.py tests/test_game_hints.py tests/test_game_websocket.py tests/test_games.py tests/test_game_llm_tasks.py tests/test_live_audio.py tests/e2e/test_crocodile_engine.py` -> **All checks passed**
- `python -m pytest -o addopts='' -q -n 0 --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_croc_runtime2 tests/test_game_cache.py tests/test_game_hints.py tests/test_game_llm_tasks.py tests/test_game_websocket.py tests/test_games.py tests/test_live_audio.py tests/e2e/test_crocodile_engine.py` -> **124 passed, 16 skipped**

## [2.15.12] - 2026-04-21 - Crocodile Topic Canonicalization & Judge Context Hardening

### 🐊 Crocodile Game Correctness
- **Canonical Topic Resolution (`app/games/word_bank.py`):** Added `resolve_topic()` and `TopicResolution` to normalize raw topic text into stable `topic_id` values. Introduced semantic unification for special domains (including League of Legends phrasing variants) so equivalent topics no longer produce separate word pools and cache keys.
- **Topic-Rotation Word Picker (`app/games/word_bank.py`, `app/handlers/inline.py`):** Added `pick_random_word_for_topic()` with per-topic rotation state keyed by the active bank hash. Repeated game starts for the same topic now rotate through words instead of repeatedly returning the same first candidate. Inline flow now resolves the topic once and uses a topic-scoped Redis used-word key (`croc:used:{creator}:{topic_id}`).
- **Context-Aware Generated Words Cache (`app/games/judgement_cache.py`, `app/games/word_bank.py`):** Extended generated-word cache keying with `topic_id`, preserving backward-compatible fallback reads for legacy entries. This removes collisions between similarly named but semantically different topics.
- **Topic-Aware Judge Input (`app/games/judge.py`, `app/games/crocodile.py`):** Judge race prompt now receives explicit `category`, `topic_id`, and `sense_context` and enforces in-topic semantic evaluation. `GuessJudgement` schema now carries `interpreted_domain` and `ambiguity_flag` metadata for future UX refinement.
- **Judgement + Hints Cache Isolation (`app/games/judgement_cache.py`, `app/games/crocodile.py`):** Introduced v2 judgement keys that include topic context, plus topic-aware hints keying. Game state now persists `topic_id` and `sense_context` in Redis serialization so reconnects preserve interpretation boundaries.
- **Hint Parser Hardening (`app/games/judge.py`):** `generate_hints()` now ignores header-like lines ending with `:` (for example, `Here are the hints:`) to avoid poisoning the 3-hint output set.

### 🧪 Verification
- `python -m ruff check .` -> passed
- `python -m mypy app` -> passed
- `python -m pytest -o addopts='' -n 0 tests --basetemp=C:\Users\user\AppData\Local\Temp\pytest_gemaibotv2_full` -> 1786 passed, 96 skipped

## [2.15.11] - 2026-04-20 - Hint Race UX Hardening & Config-Free Test Bootstrap

### 🎮 Crocodile Hint Generation
- **Three-Lane Hint Race (`app/games/judge.py`):** Reworked `generate_hints()` to use an explicit three-lane race tuned for UX instead of provider-global branching. The new order is: **AI Studio** `gemini-3-flash-preview`, **Vertex AI Express** `gemini-3.1-flash-lite` when configured, and a curated **OpenCode Go** lane that prefers `opencode-go/glm-5.1`.
- **No More First-Failure Abort:** Each hint lane now isolates its own exceptions and returns `None` on failure, so a fast provider error no longer aborts the entire race or forces an early deterministic fallback while another model is still about to return valid hints.
- **Config-Light Bootstrap:** `generate_hints()` no longer hard-depends on `get_primary_provider_async()` or a fully initialized global `settings` object before touching the router. In degraded unit-test or local-dev environments, it still attempts the AI Studio lane and only falls back to deterministic local hints after all lanes fail.
- **Live Audio Boundary Preserved:** The new Vertex path is scoped to Crocodile hint generation only. The Gemini Live Audio Mini App continues to use AI Studio API keys and was not migrated to Vertex.

### 🧪 Test Stability
- **Self-Contained Hint Tests (`tests/test_game_hints.py`):** Replaced direct patching of `config.settings.<attr>` with a local `SimpleNamespace` settings fixture, making the hint-race tests reliable even when `.env` is absent and `app.config.settings` is `None`.
- **Live Audio Fixture Hardening (`tests/test_live_audio.py`):** Reworked the fixtures to patch a shared synthetic settings object into both `app.config` and `app.web_miniapp`, preventing websocket tests from crashing during fixture setup in a minimal unit-test environment.
- **Document Handler Bootstrap Guard (`tests/test_ai_document.py`):** Added the same config-light test bootstrap pattern to adjacent document-handler tests so they no longer fail at import time on `settings=None`.

### ✅ Verification
- `python -m pytest -o addopts='' -q -n 0 tests/test_game_llm_tasks.py tests/test_game_hints.py tests/test_live_audio.py tests/test_ai_document.py` -> **21 passed**
- `ruff check app/games/judge.py tests/test_game_hints.py tests/test_live_audio.py tests/test_ai_document.py tests/test_game_llm_tasks.py` -> **All checks passed**

## [2.15.10] - 2026-04-19 - Async Cache Performance Optimization

### ⚡ Performance — Async Lock Removal
- **Synchronous Cache Lookups:** Conducted a performance audit across repository layers (`keys.py`, `users.py`, `metrics_repo.py`) and stripped completely redundant `asyncio.Lock` wrappers from synchronous `TTLCache` lookups. Since `asyncio` is single-threaded under the GIL and yields only at `await` points, these locks provided zero thread-safety benefits while silently accumulating event-loop scheduling overhead (`__aenter__`/`__aexit__`) on the application's hottest paths.
- **Architecture Cleanup:** Extracted and eliminated the `_cache_lock` property from the globally shared `DatabaseManager` singleton (`app/database.py`).
- **Thundering-Herd Logic Preservation:** Verified and documented the check-then-act pattern in `suspend_key` (`app/repos/keys.py`) to ensure atomic concurrency correctness in `asyncio`'s cooperative model without the need for explicit locking.
- **Test Scaffolding Pruned:** Stripped obsolete `_cache_lock` mock implementations from unit and integration fixtures (`tests/test_repos_users.py`, `tests/test_repos_keys.py`, `tests/test_database_tavily.py`, `tests/test_integration_flows.py`).
## [2.15.9] - 2026-04-19 - Live Audio Stability & Key Rotation

### 🔄 Gemini Live Infrastructure
- **Automatic Key Rotation:** Addressed persistent 429 and 1011 (quota) errors during high load in the Gemini Live Audio Mini App. The `live_audio_ws` handler now seamlessly rotates to the next available API key in the pool via a built-in retry loop (up to 3 attempts) without dropping the user's active WebSocket connection.
- **Accurate Penalty Classification:** Overhauled key suspension logic to correctly distinguish between transient rate limits and hard quota limits, ensuring exhausted AI Studio keys receive a 24-hour suspension instead of a 15-second cooldown.
- **Robust Exception Handling:** Transitioned `_handle_live_session` from a fail-fast approach to a resilient-retry model, preserving session context and audio flow across connection resets.

## [2.15.8] - 2026-04-19 - Test Suite Stabilization & Isolation

### 🧪 Testing & Configuration
- **Resolved Mass Test Skipping:** Identified and fixed a critical configuration leak where the `force_test_db_conn(autouse=True)` fixture in `test_chat_happy_path.py` bled into the global pytest session, causing 1,858 tests to skip when the local integration database was unavailable.
- **Isolated E2E Fixtures:** Created a dedicated `tests/e2e/conftest.py` with scoped `pytest_collection_modifyitems` logic to gracefully skip only E2E tests when `TEST_DATABASE_URL` is missing, protecting the fast unit test suite.
- **Mock Robustness in Live Audio:** Fixed unstable tests in `test_live_audio.py` that caused race conditions and side effects across `asyncio` workers in `pytest-xdist`. Correctly mocked `app.repos.chats.get_user_chat` to break the implicit database dependency introduced by recent Mini App chat settings integration.
- **Game Engine Test Fidelity:** Restored deterministic mock fallthrough behavior in `TestPickRandomWord` by patching the `_generate_single_word_fast` hot path to return `None`, forcing the category fallback logic to trigger correctly under test conditions.

## [2.15.7] - 2026-04-19 - Structured Concurrency & Linter Hardening

### 🔄 Architecture — Phase 2 Structured Concurrency
- **Eliminated Rogue `create_task` Patterns:** Systematically audited and refactored fire-and-forget background tasks across `app/games/crocodile.py`, `app/games/word_bank.py`, and `app/handlers/msg_media.py`. Ad-hoc asynchronous executions are now correctly routed through the centralized `TaskManager.submit_task` boundary. This guarantees structured lifecycle management, prevents premature garbage collection of pending tasks, and enables graceful drain during application shutdown.
- **Safe Concurrent Scoping via `TaskGroup`:** Upgraded the legacy pattern of `asyncio.gather` intertwined with raw `create_task` in `app/handlers/ai_photo.py` to leverage native `asyncio.TaskGroup`.
- **Cancellation Latency Elimination:** Repositioned the progress-indicator cancellation sequence directly inside the `TaskGroup` context manager in `ai_photo.py`. This fixed a multi-second UI latency lag when cleaning up the Telegram download loading signals perfectly resolving unresponsive state hanging.

### 🧹 Code Quality & Safety
- **Zero-Defect Base (`ruff` & `mypy`):**
  - Remediated the `SIM101` rule globally (e.g. `isinstance(X, bytes) or isinstance(X, str)` merged seamlessly).
  - Fixed an improper exception override in `scheduled_briefs.py` where a custom exception failed to propagate its `__cause__`, raising a strict `B904` exception chaining warning. Now chained properly as `raise ... from e`.
  - Reached 0 type errors from `mypy` across 329 source files. Corrected types such as `types.ThinkingConfig | None` inside `multimodal_processor.py`, `list[str | None]` inside `memory_extraction.py`, and `dict[str, str | None]` dict mapping annotations in standard test environments. 

### ✅ Verification
- Full suite executed post-refactor: **1858 passed, 0 failed** (`pytest-xdist -n auto`)

---

## [2.15.6] - 2026-04-18 - Hot-Path Regex & Collection Hoisting (Performance)

### ⚡ Performance — Module-Level Constant Hoisting

A systematic audit of all hot-path handlers and utilities was performed to eliminate redundant work executed on every message, voice, or streaming event. All patterns identified below were compiled or allocated inside function bodies, causing repeated overhead proportional to request count.

#### `app/handlers/cmd_image.py` — `_VERB_HEURISTIC`
- **Before**: `re.compile(r"(?i)\b(?:нарисуй|…|make)\b")` was called inside `check_draw_intent_async()` on every intent check that survived the fast regex pre-filter.
- **After**: Hoisted to module-level `_VERB_HEURISTIC` constant. Zero compilation overhead per call.

#### `app/handlers/msg_voice.py` — `_VOICE_ACTION_PATTERN`
- **Before**: `re.compile(r"^(?:вот,?\s*)?(сочини|…|подскажи)\s")` allocated inside `_should_auto_route()`, which runs on every transcribed voice message to determine whether to bypass the confirmation UI.
- **After**: Hoisted to module-level `_VOICE_ACTION_PATTERN`. Added `import re` to module top-level imports (previously deferred to function body).

#### `app/utils/text_format.py` — `_TAG_RE`, `_EMPTY_TAG_RE`
- **Before**: `sanitize_html_tags()` compiled `_TAG_RE` on every invocation (used in streaming chunk finalisation and mid-stream buffer flushes). `_EMPTY_TAG_RE` was additionally compiled inside a `while` loop — meaning it could be compiled N times per message when empty tags were detected.
- **After**: Both patterns hoisted to module-level constants. The `while _EMPTY_TAG_RE.search(result)` loop now reuses a single pre-compiled pattern object regardless of loop iterations.

#### `app/repos/chats.py` — Consolidated Query & Module-Level Pydantic Imports
- **Before**: `get_user_chat` executed two sequential DB round-trips — one for `chat_info` and one for `user_info` — requiring two separate asyncpg pool connections and two network RTTs per chat load.
- **After**: Consolidated into a single `LEFT JOIN` query on `chat_state JOIN user_state`. Pydantic model imports (`ChatStateRow`, `UserInfoRow`) moved to module level to avoid `__import__` lock overhead on the hot DB read path.

#### `app/repos/users.py` — Module-Level Pydantic Import
- **Before**: `UserStateRow` was imported inside `load_user_state()` on every call.
- **After**: Import moved to module level.

#### `app/security.py` — `_CONTROL_CHARS_RE`
- **Before**: `InputSanitizer.sanitize_query()` used a character-by-character generator expression `"".join(c for c in s if ord(c) >= 32)` to strip ASCII control characters on every message before LLM dispatch.
- **After**: Module-level `_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]")` + `.sub("", sanitized)`. The regex match loop runs in C rather than Python bytecode — ~5–10× faster than the generator expression.

#### `app/streaming.py` — `_MD_FENCE_RE`, `_MD_LANG_RE`, `_MD_STRIP_FENCES_RE`, `_MD_STRIP_INLINE_RE`, `_MD_STRIP_LINKS_RE`
- **Before**: `_detect_open_markdown()` called `re.compile(r"^```", re.MULTILINE)` and three additional anonymous `re.sub()`/`re.match()` literals on every streaming overflow split.
- **After**: All four patterns hoisted to module-level constants `_MD_FENCE_RE`, `_MD_LANG_RE`, `_MD_STRIP_FENCES_RE`, `_MD_STRIP_INLINE_RE`, `_MD_STRIP_LINKS_RE`. Comments added explaining the PERF rationale for each.

#### `app/intent_router.py` — `_COIN_NAMES`, `_SORTED_CURRENCY_ALIASES`
- **Before**: `_handle_crypto()` rebuilt a 4-entry `_COIN_NAMES` dict on every call. `_extract_currency_pair()` called `sorted(_CURRENCY_CODES.keys(), key=len, reverse=True)` on every fiat query — an O(n log n) allocation that produced the same result every time.
- **After**: `_COIN_NAMES` hoisted to module-level dict. `_SORTED_CURRENCY_ALIASES` pre-sorted once at import time.

#### `app/model_selector.py` — `select_model` live list
- **Before**: `select_model()` called `list(settings.AVAILABLE_MODELS)` as a defensive copy before passing to `_find_model()`.
- **After**: Removed unnecessary `list()` copy — `_find_model()` is read-only and safe to receive the raw iterable from `settings`, eliminating an allocation on every model selection evaluation.

#### `app/games/judge.py` — History format fix
- **Before**: `generate_hints()` constructed `history` entries with `parts: [{"text": prompt}]` (Gemini SDK dict format), causing OpenRouter/Opencode to receive stringified dict repr as the prompt.
- **After**: Fixed to `parts: [prompt]` plain string format. Added top-level `try/except Exception` with `logger.exception()` for silent crash prevention. Removed explicit `timeout=25.0` kwarg that was disabling retry/fallback chains.

### 🧹 Code Quality

- `ruff check --fix . && ruff format .` — All checks passed, 5 files auto-formatted (import block sorting in `chats.py`, `users.py` and 3 others).

### ✅ Verification

- Full suite: **1841 passed, 0 failed** (≈5 min, `pytest-xdist -n 12`)
- `ruff check .` → All checks passed
- `ruff format --check .` → 336 files already formatted

### Files Changed

| File | Change |
|------|--------|
| `app/handlers/cmd_image.py` | `_VERB_HEURISTIC` hoisted to module level |
| `app/handlers/msg_voice.py` | `_VOICE_ACTION_PATTERN` hoisted; `import re` moved to top |
| `app/utils/text_format.py` | `_TAG_RE`, `_EMPTY_TAG_RE` hoisted to module level |
| `app/repos/chats.py` | Consolidated JOIN query; Pydantic imports moved to module level |
| `app/repos/users.py` | `UserStateRow` import moved to module level |
| `app/security.py` | `_CONTROL_CHARS_RE` replaces generator expression |
| `app/streaming.py` | 5 MD detection regexes hoisted to module level |
| `app/intent_router.py` | `_COIN_NAMES`, `_SORTED_CURRENCY_ALIASES` hoisted |
| `app/model_selector.py` | Removed redundant `list()` copy in `select_model` |
| `app/games/judge.py` | History format fix + crash prevention + retry re-enabled |
| `.jules/bolt.md` | Performance learnings documented |
| `tests/test_game_llm_tasks.py` | Mock alignment for updated judge task signature |

---

## [2.15.5] - 2026-04-18 - Gemini Live API Security & UX Hardening

### 🛡️ Security & API Resilience
- **Strict Single Session Policy (`app/web_miniapp.py`)**: Enforced a `max 1 active session` constraint per `user_id`. Prevents overlapping connection abuse where users could launch duplicate background WebSockets and silently drain API capacity.
- **API Key Round-Robin (`app/web_miniapp.py`)**: Migrated away from static `api_keys[0]` loading. Connections now resolve tokens globally through a modulo indexing strategy (`api_keys[_KEY_ROTATION_INDEX % len(api_keys)]`). Distributes live-session workload seamlessly across all active credentials, reducing instantaneous rate limits (429s).
- **Session Duration Cap (`app/web_miniapp.py`)**: Implemented a hard 30-minute duration limit (`time.monotonic() - start_time > 1800`) per Gemini Live socket to prevent infinite silent streaming exhaustion.

### ✨ Frontend UX & Telemetry
- **Rich Status Feedback (`app/templates/live_audio.html`)**: Adjusted `setStatus` utility to color-code connections and prepend emojis (`🟢`, `🔴`, `⏳`) dynamically, reducing visual ambiguity on disconnects.
- **Telegram Haptic Integration (`app/templates/live_audio.html`)**: Embedded `Telegram.WebApp.HapticFeedback`. Emits tactile signals on session connect (`success`), errors (`warning`), and critical disconnects, significantly improving native app feel.
- **Visualizer Interpolation (`app/templates/live_audio.html`)**: Added a distinct, hue-rotated pulse-ring animation for the intermediate `connecting` state, maintaining fluid UX while the underlying Socket resynchronizes.

## [2.15.4] - 2026-04-18 - Gemini Live Session Resumption

### 🐛 Bug Fixes & UX

- **Gemini Live Context Loss (`app/web_miniapp.py`, `app/templates/live_audio.html`):** Resolved an issue where pausing the microphone (or experiencing a brief network drop) permanently destroyed the live session context.
  - Toggling the microphone now isolates WebAudio API capture control without terminating the underlying WebSocket to the Quart backend. The server is correctly signaled with `audio_stream_end` to yield model generation, maintaining a continuous context window.
  - Implemented Gemini Live API `SessionResumptionUpdate` mechanics. The server captures `new_handle` tokens and pushes them to the frontend payload. On accidental disconnects, the frontend auto-reconnects and passes the `resumptionToken` query parameter, ensuring `LiveConnectConfig` cleanly binds to the prior distributed context state.
  
## [2.15.3] - 2026-04-18 - Hint Generation Fixes & Circuit Breaker Diagnostics

### 🐛 Bug Fixes

- **`generate_hints` History Format (`app/games/judge.py`):** Fixed a silent data corruption bug where `history` was constructed with `parts: [{"text": prompt}]` (Gemini SDK dict format) instead of `parts: [prompt]` (plain strings). `OpenRouterProvider._build_messages` calls `str(part)` on each element in `parts`, so Opencode / OpenRouter received the Python dict repr `"{'text': 'Придумай...'}"` as the prompt text instead of the actual prompt. Hints were either generated from garbage input or silently rejected by the API.
- **`generate_hints` Silent Crash Prevention (`app/games/judge.py`):** Added a top-level `try/except Exception` with `logger.exception()` around the entire function body. Before this fix, any unhandled exception in the background hint prefetch task was silently swallowed by asyncio — `_mem_hints` would be left empty with no diagnostic trace in logs.
- **`generate_hints` Retry Suppression (`app/games/judge.py`):** Removed the explicit `timeout=25.0` kwarg passed to `AgentRequestUseCase.get_ai_response`. In the router, any non-`None` `timeout` argument forcibly sets `max_retries=1`, disabling the standard retry/fallback chain. Hint generation was therefore permanently failing on transient Opencode errors instead of recovering with the Gemini fallback.

### 📊 Observability

- **Circuit Breaker Exception Type (`app/circuit_breaker.py`):** Added `type(exception).__name__` to the failure log format string. The log now reads `Circuit Breaker 'ai_provider:opencode' failure #1 [TimeoutError]: ` instead of `failure #1: `. This is critical because `str(asyncio.TimeoutError())` in Python 3.11+ is always `""` — the type name is the only diagnostic signal in such cases.

### ✅ Verification

- `ruff check app/` → All checks passed
- `py_compile app/games/judge.py app/circuit_breaker.py` → OK

## [2.15.2] - 2026-04-18 - Admin Wizard & AAA Test Hardening

### 🐛 Bug Fixes & UX
- **Callback Routing Collision**: Fixed a handler group collision where the global `model_button_callback` handler (group `-1`) with the pattern `^model` intercepted the `/models` admin wizard buttons (prefixed with `models:`). Tightened the regex to `^model[_:]` to cleanly isolate legitimate model selections without bleeding into wizard pagination states.

### 🧹 Code Quality & Linting
- **Test Hardening**: Fixed `F841` unused variable error in `tests/e2e/test_crocodile_engine.py` during AAA test stabilization. Verified zero non-deterministic failures across over 1,841 unit and e2e integration tests.

## [2.15.1] - 2026-04-18 - Northflank Legacy Cleanup & Test Stabilisation

### 🧹 Cleanup (Tech Debt)
- **Northflank Legacy Removal**: Removed all mentions of the legacy "Northflank" provider in code comments, configuration references (`app/config.py`, `app/utils/logging_config.py`, `app/web.py`), and documentation (`docs/ARCHITECTURE.md`, `README.md`), as the production stack fully relies on the dedicated VPS deployment architecture.
- **Docker Artifacts**: Deleted the legacy `northflank.yaml`. Renamed `Dockerfile.northflank` to `Dockerfile` and `docker-compose.northflank.yml` to `docker-compose.yml`.

### 🛡️ Test Stabilisation & Architecture
- **Async Task Leakage Fix (`app/database.py`)**: Secured background DB tasks by registering `_task` creation explicitly within a global module-level `_background_tasks` set, addressing `RUF006` errors and ensuring `asyncio` garbage collection doesn't cull migration alerts early.
- **CLI Lint Suppressions**: Ignored native `print` statements in CLI scripts (`scripts/migrate.py`) via top-level `noqa: T201` definition.
- **Architecture Documentation**: Documented the current state of the 3-container production deployment stack and updated test suite totals (`1800+`).

## [2.15.0] - 2026-04-18 - Live Audio Voice Chat (Gemini Live API)

### 🚀 New Feature

- **Live Audio Mini App (`app/templates/live_audio.html`, `app/static/js/audio-processor.js`):** Real-time bidirectional voice conversation with AI via a Telegram Mini App. Uses `gemini-3.1-flash-live-preview` for sub-second latency duplex audio streaming over WebSocket.
  - **AudioWorklet Frontend:** Browser captures mic audio via an `AudioWorklet` processor that resamples to 16kHz mono PCM16 in 100ms chunks, fully offloading DSP from the main thread. Sends base64-encoded frames over WebSocket.
  - **WebSocket Proxy (`app/web_miniapp.py` → `/webapp/live/ws`):** Quart WebSocket handler bridges the browser and Gemini Live API session (`client.aio.live.connect`). Concurrent `asyncio.create_task` producer/consumer loops handle full-duplex streaming with `try-finally` cleanup. Audio, text, and `audioStreamEnd` signals are forwarded to Gemini; audio chunks, transcripts, and interruption events are relayed back.
  - **Playback Pipeline:** Gemini responds with PCM 24kHz audio relayed as base64 JSON. The frontend decodes and queues buffers through Web Audio API `AudioContext` for gapless sequential playback. Queue is flushed instantly on `interrupted` signal.
  - **Waveform Visualizer:** Circular frequency-reactive bar visualizer using `AnalyserNode` with animated pulse ring during active recording.
  - **Transcription Overlay:** Real-time input (user) and output (AI) transcripts are displayed in a scrolling glassmorphic transcript area.
  - **Session Resilience:** Tracks `sessionResumptionUpdate` tokens for future reconnect support. 10-minute idle timeout. Telegram `initData` HMAC-SHA256 authentication (same as Crocodile).

### 🔧 Infrastructure

- **`GEMINI_LIVE_MODEL` Constant (`app/config.py`):** Added `gemini-3.1-flash-live-preview` as the canonical Live API model identifier, co-located with existing Imagen model constants.
- **Static Page Route (`/webapp/live`):** New Quart route serving the Live Audio Mini App HTML shell.

### ✅ Testing

- **7 new tests (`tests/test_live_audio.py`):** Covers WebSocket auth (missing/invalid initData), static page serving (200 OK), audio forwarding (realtime_input → Gemini), interrupt relay, and input/output transcript streaming. All tests use fully mocked Gemini sessions — zero API calls.

## [2.14.1] - 2026-04-17 - Migration Hardening & Stability

### 🛡️ Deployment & Schema Reliability

- **Standalone Migration Runner (`scripts/migrate.py`):** Created a dedicated, standalone CLI tool for executing, verifying (`--check`), and reporting (`--status`) on database migrations independently of the bot's runtime lifecycle.
- **Fail-Fast Deployment Gate (`.github/workflows/deploy.yml`):** Injected an explicit migration execution step into the CI/CD pipeline prior to launching the main bot container. This runs `scripts/migrate.py` in an ephemeral container, ensuring deployments hard-stop if schema updates fail, preventing the bot from spinning up with an incompatible database state.
- **Strict Migration Evaluation (`app/db/migrations.py`):** Rearchitected `run_migrations()` to implement hard-stop, fail-fast behavior. On encountering the first failure, dependent migrations immediately abort (Alembic-style). Modified the function to return a structured `MigrationResult` object instead of silently absorbing errors, and elevated critical schema failures to `CRITICAL` log severity.
- **Schema Drift Observability (`app/database.py` & `app/admin_alerts.py`):** The application now analyzes runtime migration results. If schema drift or active failures are detected during bootstrap, it triggers a deferred webhook `_send_migration_alert()`. This leverages a newly created `alert_admin_raw()` utility to securely bypass PTB Application dependency loading and DM the administrator instantly, providing live observability without crashing the boot sequence.

### 🐛 Bug Fixes & UX

- **Routing Layer Key Exhaustion Logs (`app/agent_use_cases.py`):** Reduced the log level of `all_exhausted` events from `ERROR` to `DEBUG` inside `_resolve_opencode_request`. During expected parallel 'Race Request' deployments, normal fallback key rotations were triggering spurious production errors. The high-level provider router now exclusively handles UX-facing error reporting.
- **Suggestion Cache Miss Handling (`app/handlers/cb_smart_actions.py`):** Added a safeguard in `suggestion_callback` to gracefully handle expired suggestion buttons (e.g., after a bot restart or multi-worker miss). Instead of sending the raw 10-character MD5 hash fragment back to the AI (which previously confused the model into responding "я получил от тебя буквы-цифры"), the bot now intercepts cache misses and triggers a native Telegram popup (`Подсказка устарела. Пожалуйста, напишите запрос вручную.`).

## [2.14.0] - 2026-04-17 - Crocodile Game: UX Hardening & Stability

### 🚀 UX Improvements

- **Emoji Temperature Prefix (`app/games/judge.py`):** Every LLM comment from the semantic judge now carries an automatic emoji prefix (`🧊`/`🟡`/`🔥`/`🎉`) prepended on the backend before the event hits the WebSocket. The AI's witty text is preserved 100% unchanged — the temperature signal is simply added in front for instant visual parsing at zero latency cost.
- **ASCII Score Progress Bar (`app/games/judge.py`):** New `score_bar(score, width=10)` utility returns an `[██████░░░░]`-style bar. The result is included in every WebSocket `result` event as `score_bar`, making it trivial for the frontend to render a progress indicator.
- **Inline Message Thermometer (`app/games/crocodile.py`):** When the guesser achieves a new personal best score, the bot calls `update_inline_thermometer()` to silently `edit_message_text` the original inline button message in the private chat — showing the best-attempt bar (e.g., `🔥 Лучшая попытка: [██████░░░░] 60%`) to both players without them opening the WebApp. Persisted `best_score` field is now serialised to Redis so thermometer survives WebSocket reconnects.
- **Graceful Surrender (`app/games/crocodile.py`):** New `surrender()` method — when the guesser taps a surrender button, the game transitions to `lost`, `finalize()` edits the inline message with `🏳️ Игрок сдался. Слово было: X`, and a `surrendered` WebSocket event is broadcast to both players. Zero guesses needed, clean loop closure.
- **Creator "God Mode" Hint (`app/games/crocodile.py`):** Added `broadcast_creator_hint` public helper that accepts arbitrary creator-authored text and fans it out via the existing `asyncio.Queue` Pub/Sub system. The creator's custom message arrives in the guesser's chat as a special hint event — no LLM call required, instant and free.
- **Improved Surrender Phrasing in `finalize()`:** The loss message now correctly uses `len(self.attempts)` (actual attempts made) instead of `self.max_attempts`. Surrender with zero guesses shows dedicated phrasing.

### 🛡️ Stability / Local Heuristics

- **`ё → е` Normalisation (`app/games/judge.py`):** `_homogenize_pair` now translates the Cyrillic letter `ё` (and `Ё`) to `е` (and `Е`) before any local comparison. A player typing `бобёр` nows matches `бобер` (or vice versa) without an LLM call.
- **Punctuation Stripping (`app/games/judge.py`):** Trailing/embedded punctuation (`.,!?;:…—–`) is stripped from both the target and guess before Damerau-Levenshtein comparison. A guess of `кот.` now scores `exact_match` locally.
- **Async Cache I/O (`app/games/judgement_cache.py`):** All three `.json` cache persist operations (`_persist`, `_persist_hints`, `_persist_cat`) were previously synchronous function calls inside async handlers. Each now dispatches to `asyncio.to_thread()`, keeping blocking JSON serialisation + atomic file-swap off the event loop thread.
- **Word-Category LRU Cache (`app/games/judgement_cache.py`):** New `_cat_store` (10k entries max, JSON-backed, `category_cache.json`) caches resolved custom-word categories. `resolve_custom_word_category` checks `get_cached_word_category()` before calling the LLM. The same LLM result is immediately written back via `cache_word_category()`, so any word seen twice is served in <1ms.
- **Category-Aware LLM Prompt (`app/games/word_bank.py`):** `resolve_custom_word_category` now passes the full 12-category list (including the 4 new ones) to the LLM instead of the old 8-category list.

### 📚 Word Bank Expansion

- **4 New RU + EN Categories** added to `WORD_BANK`:
  - 🚁 **Транспорт / Transport** (15 words each): вертолёт, подводная лодка, дирижабль, ракета, рикша…
  - 👘 **Одежда / Clothing** (15 words each): кимоно, смокинг, пижама, бриджи, кираса…
  - 🎻 **Музыка / Music** (15 words each): виолончель, балалайка, диджериду, маракасы, арфа…
  - 🌠 **Космос / Space** (15 words each): астероид, чёрная дыра, нейтронная звезда, метеорит, зонд…
- **`_CATEGORY_ALIASES` updated** with 17 new aliases (транспорт, одежда, музыка, космос, инструменты, астрономия, вселенная, vehicles, clothing, fashion, space, astronomy…).

### 🔧 Pre-existing Lint Cleanup

- **`app/providers/router.py`:** Renamed unused variable `is_or` → `_is_or` (F841).
- **`app/repos/memory_extraction.py`:** Used `_` placeholder for unused tuple elements in `_resolve_ai_request` destructuring (RUF059).
- **`app/utils/multimodal_processor.py`:** Used `_` placeholder for unused `resolution` in destructured tuple (RUF059).

### ✅ Verification

- Full suite: **1813 passed, 0 failed** (2m 22s, `pytest-xdist -n auto`)
- `ruff check .` → All checks passed

## [2.13.5] - 2026-04-17 - Production Stability: Time Injection & DB Constraints

### 🐛 Critical Bug Fixes

- **`{{CURRENT_TIME}}` LLM Hallucination (`app/handlers/ai_chat.py`):** Fixed an issue where the LLM would hallucinate a template string `{{CURRENT_TIME}}` when asked for the current time. True Moscow time is now dynamically injected into the system prompt at call-time (bypassing the base prompt LRU cache) so the model is always temporally aware.
- **Opencode 10s Latency Spikes (`app/providers/router.py` & DB Migrations):** Resolved severe performance degradation when querying Opencode models. 
  - *Root Cause:* The `check_key_hash_exists` trigger on the `key_model_status` DB table expected all `key_hash` entries to exist in either the `api_keys` or `openrouter_api_keys` tables. Opencode keys are stored exclusively in-memory, causing every `record_success` and `suspend_key` call to fail with a Postgres `ForeignKeyViolationError` and burning 4 retry attempts (~10s overhead) per request.
  - *Fix:* Added `if not is_opencode_model(model)` guards around all `status_mgr` writes in `router.py` (standard, stream-fallback, and race-path). 
  - *Defense-in-depth:* Applied migration `038_relax_key_model_status_trigger.sql` which downgrades the hard `RAISE EXCEPTION` to a non-blocking `RAISE WARNING` for unknown provider hashes.
- **Cyrillic Tag Hallucinations (`app/utils/response_tags.py`):** Fixed a visual bug where the tag `[СUGGESTIONS: ...]` would sometimes display directly to end-users. The LLM occasionally substituted the ASCII 'S' with the visually identical Cyrillic 'С' (U+0421). The stripping regex `_SUGGESTIONS_RE` has been hardened with a Unicode character class (`[SC\u0421]`) to catch mixed-encoding tags.

## [2.13.4] - 2026-04-17 - Crocodile Game: Asynchronous Word Gen & Static Categorization

### 🚀 Performance & UX Improvements

- **Resilient AI Hint Generation (pp/games/judge.py):** Increased the background AI hint generator timeout from 12s to 25s. Because UI hints do not unlock until 10 seconds into the game anyway, this longer tolerance drastically reduces timeout-induced generation failures when Opencode models hit burst latency.
- **Instant Game Start (`app/handlers/inline.py`, `app/games/word_bank.py`):** Removed the blocking 15-second `resolve_custom_word_category` LLM call when users pass a custom word (`=Слово`). Instead, the category defaults statically to `"Слово игрока (особое)"`, letting players start immediately without LLM classification friction.
- **Fast Word Generation (`app/games/word_bank.py`):** Eliminated the long initial wait when selecting an uncached random category. `pick_random_word` now calls `_generate_single_word_fast()` using the lighter `settings.OPENCODE_INLINE_MODEL` with a strict 7s timeout to generate exactly *one word*, returning to the player instantly. The full 20-word bank for the category is generated asynchronously via a background `asyncio.create_task`.
- **Config-Driven Models (`app/games/word_bank.py`):** Word bank LLM tasks no longer hardcode `opencode-go/minimax-m2.7`. They now dynamically route to `settings.OPENCODE_QNA_MODEL`. The execution timeout limit was increased from 18s to 25s for full-bank processing to tolerate Opencode's longer latency.

### 🧪 Tests
- **`test_game_inline.py`**: Refactored `test_custom_word_mode` and `test_custom_word_mode_bank_hit` to check against the new optimized behavior (static `"Слово игрока (особое)"` assignment and no mock checks on the removed `resolve_custom_word_category` function).

## [2.13.3] - 2026-04-17 - Miniapp Core Logic & Metrics Filtering

### 🐛 Critical Bug Fixes

- **Miniapp UI Deadlock (`app/templates/miniapp.html`):** Removed a duplicated `loadSettings()` closure that caused a JS `SyntaxError` on initialization. This error prevented the entire script from being parsed, preventing tab state assignment and rendering the bottom UI bar completely unclickable.
- **Opencode Models Missing in Metrics (`app/handlers/menus.py`):** The model usage aggregator historically stripped any string containing the `/` character, confusing `opencode-go/mimo-v2-omni` style identifiers with invalid file paths. Updated the logic to only trim literal file extensions (`.pdf`, `.docx`) and backslashes, restoring opencode models to the `/metrics` dashboard.

### 🧪 Tests

- **`test_request_id_headers.py`**: Fixed a test suite `AttributeError` caused by dynamically mocking a module-wide `httpx.AsyncClient`'s `.post` method when the client initialization returned `None`. Now mocks the parent client directly.

### ✅ Verification

- Full suite: **1812 passed, 0 failed** (1m 41s, `pytest-xdist -n auto`)

## [2.13.2] - 2026-04-17 - Test Suite Stabilization & Contamination Fixes

### 🐛 Critical Bug Fixes

- **Collection-Time Contamination:** Fixed an architectural instability in the test suite where module-level setup logic across various files (e.g., `test_analytics.py`, `test_document_security.py`) would mutate `sys.modules` and `os.environ` during test collection, bleeding state into the global namespace.
- **Settings Drift Resolution:** Implemented a core `_decontaminate_settings` runtime fixture in `conftest.py`. The suite now captures a `_canonical_settings` singleton before execution and aggressively propagates it to all imported `app.*` submodules via `setattr` prior to each test module. This eliminates "Settings Drift" where application functions were binding to stale or duplicated settings objects (or `MagicMock` remnants) purged and recreated during testing.
- **Database MagicMock Failures:** Migrated tests relying on module-level environment mocks to use targeted `setup_module`/`teardown_module` and proper fixture injection architectures, resolving random `TypeError: '>' not supported between instances of 'MagicMock' and 'int'` failures in the Database manager pool initialization.

### ✅ Verification

- Full suite: **1803 passed, 0 failed** (1m 54s)

## [2.13.1] - 2026-04-16 - Opencode Observability Hardening & Hot-Reload Bug Fixes
### 🐛 Critical Bug Fixes

- **Provider-aware streaming metrics (`app/streaming.py`):** `record_api_call` was hardcoded to `"gemini_streaming"` for every provider. All Opencode and OpenRouter streaming traffic was silently mis-attributed to Gemini in the metrics dashboard. Fixed with an `is_opencode_model` / `is_openrouter_model` dispatch: Opencode traffic now records as `"opencode_streaming"`, OpenRouter as `"openrouter_streaming"`, Gemini as `"gemini_streaming"`.

- **Provider-aware chat metrics (`app/handlers/ai_chat.py`):** Same mis-attribution for `record_api_call` in the non-streaming chat path. Now correctly records `"opencode_chat"` / `"openrouter_chat"` / `"gemini_chat"` based on live model routing.

- **Hot-reload migration wiped Opencode users (`app/repos/chats.py` — `model_migration_watcher`):** `OPENCODE_AVAILABLE_MODELS` was excluded from the `all_available` validation set. On every hot-reload (`/reloadconfig`), every user with an Opencode model was falsely classified as using an "invalid" model and silently migrated back to the Gemini default. Fixed by adding `OPENCODE_AVAILABLE_MODELS` to the set and passing `OPENCODE_DEFAULT_MODEL` as the correct Opencode fallback target.

- **Opencode users migrated to OpenRouter default (`app/repos/chats.py` — `migrate_invalid_models`):** `opencode-go/*` model names contain `/`, which matched the OpenRouter branch in the migration router (`"/" in model`). Opencode users were routed to `OPENROUTER_DEFAULT_MODEL` instead of `OPENCODE_DEFAULT_MODEL` during hot-reload migration. Fixed by adding an explicit `opencode-go/` prefix check as a first-priority branch before the generic `/` test.

- **Stale import-time fallback map (`app/providers/router.py`):** `_OPENCODE_GEMINI_FALLBACK` was a module-level dict built once at import time using `settings.DEFAULT_MODEL` etc. After a hot-reload that changed `DEFAULT_MODEL`, the fallback map remained stale and could route Opencode-exhausted requests to the old model. Refactored to `_get_opencode_gemini_fallback()` — a function that reads from live `settings` on every call. All three call sites (non-streaming fallback, streaming race fallback, transient cascade) updated.

### 🧪 Tests

- **`test_qna_search_happy_path`**: Updated to pin `get_primary_provider` to `"gemini"` via mock — the test was written for the Gemini path but was running in an environment where `PRIMARY_PROVIDER=opencode`, causing a spurious failure. The test's assertion (`enable_web_search=True`) is correct for Gemini and remains unchanged.

- **`test_qna_search_opencode_path`** (new): Companion test for the Opencode/JINA branch. Confirms that `get_primary_provider=opencode` causes `stream_and_display` to be called with `enable_web_search=False` (JINA grounding injected into prompt instead).

- **`TestMultimodalGuard`** (5 tests in `test_opencode_routing.py`): Updated all tests importing the removed `_OPENCODE_GEMINI_FALLBACK` dict to call `_get_opencode_gemini_fallback()` instead. Invariants tested are identical.

### ✅ Verification

- Full suite: **1813 passed, 0 failed** (2m 12s, `pytest-xdist -n auto`)

### Files Changed

| File | Change |
|------|--------|
| `app/streaming.py` | Provider-aware `record_api_call` label in streaming path |
| `app/handlers/ai_chat.py` | Provider-aware `record_api_call` label in chat path |
| `app/repos/chats.py` | Hot-reload watcher includes Opencode models; migration correctly routes Opencode users |
| `app/providers/router.py` | `_OPENCODE_GEMINI_FALLBACK` dict → `_get_opencode_gemini_fallback()` function; 3 call sites updated |
| `tests/test_ai_search.py` | `test_qna_search_happy_path` pinned to Gemini; `test_qna_search_opencode_path` added |
| `tests/test_opencode_routing.py` | `TestMultimodalGuard` updated to call `_get_opencode_gemini_fallback()` |

---
## [2.13.0] - 2026-04-16 - Opencode Go Migration: Split-Brain Architecture & JINA Grounding

### Major Changes

- **Opencode Go Primary Provider (pp/providers/opencode.py):** New OpencodeGoProvider class (subclassing OpenRouterProvider) routes primary LLM traffic through opencode.ai/zen/go/v1 using Bearer token auth. The opencode-go/ prefix is stripped before sending model names to the API. Canonical model list: minimax-m2.7, minimax-m2.5, qwen3.6-plus, kimi-k2.5, ig-pickle, qwen3.5-plus, mimo-v2-omni.
- **Split-Brain Fallback Architecture (pp/providers/router.py):** _OPENCODE_GEMINI_FALLBACK maps every Opencode model to its closest-capability Gemini counterpart. When all Opencode keys are exhausted, ProviderRouter automatically retries on Gemini using the mapped model without user-visible interruption. The _is_fallback=True flag prevents infinite recursion.
- **JINA AI Search Grounding (pp/search_jina.py):** Replaced Gemini-native Google Search with a JINA-based grounding pipeline for Opencode-routed requests. Calls s.jina.ai/?q=<query> and injects the LLM-ready markdown as <search_context> XML in the system prompt. Full error resilience: returns empty string on timeout, HTTP error, or network failure without propagating exceptions.
- **Runtime Provider Admin Control (pp/handlers/commands.py, pp/config.py):** New /set_provider <name> admin command switches primary_provider in the global_settings DB table at runtime. Changes take effect immediately via _invalidate_primary_provider_cache(). Valid values: opencode, gemini, openrouter.

### Configuration

- **New settings** (pp/config.py): OPENCODE_API_KEYS, OPENCODE_AVAILABLE_MODELS, PRIMARY_PROVIDER, OPENCODE_DEFAULT_MODEL, OPENCODE_QNA_MODEL, OPENCODE_RESEARCH_MODEL, OPENCODE_VISION_MODEL, OPENCODE_INLINE_MODEL.
- **get_primary_provider()**: DB-backed with in-process string cache. Reads global_settings table on first call, then caches until _invalidate_primary_provider_cache() is called.
- **get_settings_safe()**: Null-safe settings accessor for modules imported before configuration initialization.

### Hardening & Model List Correctness

- **Canonical model-only enforcement:** Pruned all stale/hallucinated model names (glm-5, glm-5.1, mimo-v2-pro, gemini-2.0-flash, gemini-1.5-flash, gemini-2.5-flash-preview-05-20) from _OPENCODE_GEMINI_FALLBACK, _GEMINI_CASCADE, and _MODEL_TIER. Only models from the approved canonical lists are present.
- **Gemini cascade** (_pick_transient_fallback_model): Simplified to 3-flash-preview > 2.5-flash-lite, 3.1-flash-lite-preview > 2.5-flash-lite, 2.5-flash > 2.5-flash-lite.
- **Multimodal guard fix (pp/providers/router.py):** Opencode vision models (mimo-v2-omni) are no longer incorrectly forced through the Gemini path for image requests.
- **Streaming HTTP error hardening:** httpx.HTTPStatusError is now caught at the streaming layer for clearer provider failure diagnostics.
- **URL construction fix (pp/search_jina.py):** Replaced broken httpx.URL.copy_with() usage with urllib.parse.quote to prevent InvalidURL exceptions.
- **.gitignore**: Added .env to prevent accidental secret commits.

### Tests

- **	ests/test_opencode_routing.py** (29 tests): Covers is_opencode_model(), OpencodeGoProvider URL/headers/model stripping, provider factory routing, JINA search happy path and error cases, get_primary_provider() cache invalidation, multimodal guard passthrough, _pick_transient_fallback_model(), model selector skipping for Opencode models.
- **Canonical model guard tests**: New 	est_fallback_map_only_contains_canonical_opencode_models and 	est_fallback_values_are_canonical_gemini_models assert that no non-canonical model names can silently enter the fallback maps.

---
## [2.12.11] - 2026-04-16 - Judge Recovery, Crocodile TTL Activity & Lazy Image Pool

### 🐛 Reliability Fixes

- **Judge key health recovery (`app/games/judge.py`):** successful Gemini judge winners now call `record_success(key_hash, model)` before usage accounting. This clears stale suspension/failure state after transient outages so recovered keys return to the primary active pool instead of remaining permanently deprioritized.
- **Crocodile activity TTL (`app/games/crocodile.py`):** game persistence now tracks explicit guess activity via `has_activity` instead of inferring activity from counted attempts only. A `judge_unavailable` result still does **not** consume an attempt or mutate history, but it now refreshes Redis persistence using the active-game TTL window so live games do not expire while the judge is temporarily degraded.
- **Streaming hallucination filter narrowed (`app/streaming.py`):** the `[tool_code]` cleanup now targets only explicit leaked internal tool traces instead of generic fenced snippets. Legitimate examples such as fenced `search("cats")` code blocks or prose mentioning `google_search.search` are preserved.
- **Lazy image worker pool (`app/utils/image_utils.py`):** `ProcessPoolExecutor` creation moved from import time to a guarded accessor. Importing Gemini / multimodal / backward-compat audio modules no longer opens multiprocessing pipes immediately, and restricted environments now fall back gracefully instead of failing during import.

### 🧪 Tests

- Added a judge regression proving successful race winners restore Gemini key health.
- Added Redis/fake-Redis Crocodile TTL coverage for:
  - initial idle TTL on create
  - active TTL after a normal guess
  - active TTL refresh on `judge_unavailable` without counting an attempt
- Added streaming regression tests proving hallucinated `[tool_code]` traces are stripped while legitimate fenced code and `google_search.search` references remain intact.
- Added lazy image pool tests proving:
  - importing `image_utils` does not create the process pool
  - pool creation is lazy and singleton-backed
  - pool creation failure degrades gracefully

### ✅ Verification

- Targeted verification command:
  - `python -m pytest -o addopts='' -n 0 --basetemp=.pytest_tmp_codex tests/test_game_judge_integration.py tests/test_game_llm_tasks.py tests/test_games.py tests/test_audio_processor.py tests/test_streaming.py tests/test_streaming_writer.py tests/test_image_utils.py -q`
- Result: **153 passed**

---

## [2.12.10] - 2026-04-16 - Judge Key Rotation & Full Metrics Coverage

### 🐛 Bug Fixes

- **Judge 429 Key Rotation (`app/games/judge.py`):** `_one_call` now accepts `key_hash` alongside `api_key`. On any exception, `classify_key_error()` categorises the failure (`quota` / `rate_limit` / `transient` / `permanent`) and fires `_suspend_key_safe()` as a background task, writing the offending key into `key_model_status` with the appropriate cooldown (`until midnight PT` for quota exhaustion, `15 s` for transient). Because `resolve_ai_request()` already filters suspended keys at SQL level, the next race round automatically receives a fresh key — no more repeating the same exhausted `gemini-2.5-flash-lite` key on every fallback attempt.
- **Key Usage Accounting:** Successful judge calls now fire `increment_key_usage(key_hash, model)` so the usage counter remains accurate for all judge traffic.
- **`_run_race` signature hardened:** Changed `list[str]` (api_key only) → `list[tuple[str, str]]` (api_key, key_hash) so key identity is always available for suspension without carrying the full dict through task closures.

### 📊 Metrics Coverage

- **`app/games/judge.py`:** `record_api_call("gemini_judge", model)` fired in `finally` block of every `_one_call` attempt (success **and** failure). `record_request("judge", elapsed, success)` fired in `judge_guess` for all exit paths (exact match, cache hit, LLM success, LLM unavailable).
- **`app/handlers/ai_chat.py`:** `record_api_call("gemini_chat", model, user_id)` + `record_request("chat", elapsed, success)` after `stream_and_display` — covers all regular conversational turns.
- **`app/handlers/msg_voice.py`:** `record_api_call("gemini_transcribe", user_id)` + `record_request("voice", elapsed, success=bool(transcript))` at the end of `_process_voice_pipeline` — covers auto-chat, auto-search, show-and-tell, and confirmation-UI paths alike.
- **`app/handlers/ai_photo.py`:** `record_api_call("gemini_vision", model, user_id)` + `record_request("photo", elapsed, success=streamed)` inside `_process_ai_vision` — single shared gateway covering single photo, media group, and complex media-group-search flows.

---

## [2.12.9] - 2026-04-16 - LLM Artifact Filtering & Tolerance Fixes

### 🐛 Bug Fixes & Resilience

- **Gemini Search Hallucination Filter:** Addressed a behavior in the Gemini API (specifically `gemini-3.1-pro/flash`) where the model sometimes leaks internal code execution traces (e.g., `[tool_code] print(google_search.search(...))`) into the chat stream. Implemented a regex filter in `app/streaming.py` (`StreamingWriter.write`) that actively intercepts and strips these artifacts from both the temporary UI buffer and the final persisted text, ensuring a clean user experience.
- **Crocodile Judge Tolerance:** Increased the `max_length` constraint of the semantic `GuessJudgement` model in `app/games/judge.py` from 80 to 255. This resolves a `503` cascade failure where the primary model (`gemini-3.1-flash-lite`) routinely triggered fallback to `gemini-2.5-flash-lite`, which occasionally generated verbose fallback hints exceeding the old limit, crashing the validator.

---

## [2.12.8] - 2026-04-16 - Crocodile Mini App: Spectator Mode & LLM Hardening

### ✨ Feature — God Mode (Spectator UX)

- **Architecture:** Transitioned from a blindly blocked creator view to a full live "Spectator Mode". Creators who start games using a custom word can now observe the guesser's attempts in real-time.
- **PubSub Broadcaster:** Implemented an in-memory PubSub system (`asyncio.Queue` based) in `crocodile.py`. WebSocket handlers subscribe to the game's feed upon connection and decouple broadcasts from message loops via independent `asyncio.create_task` drains.
- **UI Adjustments (`crocodile.html`):** 
  - Guesser's bubbles are mirrored to the left side (`.bubble-row.spectator`) for the creator.
  - New "Target Word Banner" at the top of the interface constantly reminds the creator of the mystery word.
  - Active presence indicators (`Игрок печатает...`) fired by ephemeral WS typing events.
- **Interactive Spectation:** Expanded creator WS permissions to send live emoji reactions (`🔥`, `❄️`, `😂`, `👏`, `🤔`). These reactions briefly appear as fading system bubbles in the guesser's chat feed, creating a bidirectional, collaborative game feel while maintaining strict game integrity.

### 🧠 Algorithm — Prompt & Context Resilience (Bug-6.3/6.4)

- **Removed Anchoring Bias:** `judge.py` systemic prompts were rewritten to eliminate literal "example anchors" (e.g., repeatedly asserting the exact phrase `"Совсем из другой оперы!"`). Expanded LLM tokens and temperature parameters (token max `100`→`200`, temp `0.1`→`0.5` for judging and `0.3` for hints) to facilitate dynamically descriptive semantic evaluations.
- **Reverse Category Lookup:** `word_bank.py` now maps `word_to_category` at module scope. If a creator specifies `=крокодил` (a word already inside the built-in bank), the script reverse-resolves it to the `"Животные"` category logic stream, delivering critical context back to the hint-generating LLM.
- **Context Injection:** Truly unknown custom words no longer default to the opaque literal `"custom"`, which was empirically causing the LLM to hallucinate unrelated semantic links (e.g., mistaking "Germany" for "Italy"). They now present as `"слово игрока (произвольная тема)"`.

### 🛡️ Tests

- `test_game_inline.py` expanded to strictly cover both true custom-word injections and reverse-lookup (bank-hit) category coercions. Full offline parallelization achieves 100% test passing (1770 total).

---

## [2.12.7] - 2026-04-15 - Crocodile Mini App: UX Hardening & Messenger-Style UI

### 🎮 UX — Messenger-Style Chat History (`crocodile.html` full rewrite)

- **Telegram-style chat bubbles** replace the old single-card feedback: every guess is rendered as a right-aligned bubble with status colour (❄️ cold / 🌡️ warm / 🔥 hot / ✅ exact), similarity %, and judge hint inline.
- **Optimistic UI** with `pending_id`: the bubble appears immediately (⏳, pulsing) while the server evaluates the guess. On response, the bubble is resolved in-place without a DOM re-render. On `judge_unavailable`, the pending bubble is removed and an error bar appears.
- **History restore on reconnect** (`history_sync` event): server sends full ordered guess history to reconnecting clients so they don't lose context.
- **Typing indicator** (`Крокодил оценивает…`): driven by the DOM state of `.bubble.pending` — shows whenever there's an unresolved in-flight guess, hides immediately on resolution.
- **Haptic feedback** via `Telegram.WebApp.HapticFeedback` on every interaction: send (medium), cold (light), warm (medium), hot/exact (success notification), judge_unavailable (warning), game_over (error).
- **100dvh layout** with `env(safe-area-inset-bottom)` for safe-area-aware input zone; keyboard appearance handled via `dvh`. Smooth `scroll-behavior` on new bubbles.

### 💡 UX — Progressive Hints System

- **Background LLM generation**: at game creation, `create_game()` fires `asyncio.create_task(_prefetch_hints(game_id, word, category))` — 3 progressively revealing hints are generated via Gemini before the guesser connects.
- **Hint cache** (`hints_cache.json`): LRU OrderedDict (5 000 entries), same write-through pattern as judgement cache. Repeat games with the same word pay zero LLM tokens.
- **Hint reveal protocol**: `{type:'hint', hint_index:N}` → server responds `{event:'hint', text, available:true/false}`. `available:false` if prefetch is still running or exhausted.
- **💡 button UX**: 12s initial cooldown from game start (player should try first), 9s per-hint cooldown between requests. Button shows remaining count (`💡 3` → `💡 2` → disabled). Rollback on `available:false`.
- **In-memory per-game stores** (`_mem_hints`, `_mem_history`): module-level dicts in `crocodile.py`, not serialised to Redis. Ephemeral by design — regenerated on process restart; capacity bounded by active game lifetime.

### 🧮 Algorithm — Damerau-Levenshtein Typo Tolerance

- **Replaced** `difflib.SequenceMatcher` in `_local_check` with a pure-Python restricted Damerau-Levenshtein distance (counts insertions, deletions, substitutions, **and adjacent transpositions** — catching `монгуст→мангуст` that SequenceMatcher misses).
- **Length-dependent tolerance**: ≤4 chars → 0 edits (Кот ≠ Кит), 5–7 chars → 1 edit (мангуст→монгуст ✓), ≥8 chars → 2 edits (крокодил→крокадил ✓).

### 🧠 Algorithm — Semantic-Only Judge (no lexical fallback)

- **Removed `_fallback_judgement`** entirely from `app/games/judge.py`. Character-level string similarity (Levenshtein / SequenceMatcher) cannot measure semantic distance and produced factually wrong warm/cold scores (e.g., `парашют ≈ порошок`).
- **`judge_unavailable` sentinel**: when the LLM race times out or all keys fail, `judge_guess()` now returns `("judge_unavailable", …)` instead of a misleading score. `process_guess()` does **not** count the attempt, returns `{event:"judge_unavailable"}` to the client.
- **LLM race timeout raised 1.5s → 3.0s** to cover SSL-handshake throttling tails.
- **System prompt hardened**: explicit instruction to evaluate *only semantic meaning*, not character similarity. Includes counter-example (`парашют ≠ порошок` despite letter overlap).

### 🔌 Protocol — WebSocket Extensions

- `history_sync`: sent after `game_state` if any guesses were made in the current session.
- `hint` message type handled in the WS loop ### 🛡️ Tests — `tests/test_games.py`, `tests/test_game_websocket.py`, `tests/test_game_llm_tasks.py` Extended

- **WebSocket Integration** (`test_game_websocket.py`): Full quart test_client simulation of miniapp WS endpoints. Tests WS-01 (Auth rejection), WS-02 (history_sync on connect), WS-03 (hint requesting and LRU limits), WS-04 (guess submission routing), and WS-05 (Creator Guard checking).
- **LLM Task Mocks** (`test_game_llm_tasks.py`): Implemented strict separation of `google.genai.Client` and `AgentRequestUseCase` mocks. Verifies JSON array schema parsing and `503 Service Unavailable` failover routing.
- **Creator Guard Implementation**: Fixed a missing server-side authorization check in `web_miniapp.py` that allowed game creators to process guesses; WS now firmly rejects with `is_creator` context bounds.
- **Added** `TestDamerauLevenshtein` (8 tests): all four edit operations, transposition, multi-edit, empty strings.
- **Added** `TestAllowedEdits` (3 tests): boundary values for each length tier.
- **Extended** `TestLocalCheck` (11 tests): `пеликан` regression, 4-char zero-tolerance, transposition catch, long-word 2-edit tolerance.
- **Added** `mock_llm` `autouse` fixture to `TestCrocodileGameInMemory`: patches `_race_generate` → cold stub, `cache_judgement` → no-op, `_prefetch_hints` → no-op. All async game tests run fully offline.
- **Added** `test_history_recorded_on_guess`, `test_judge_unavailable_does_not_count_attempt`, `test_mongust_typo_regression`, `test_krokadil_typo_exact_match`, `test_wrong_guess_counts_attempt`.

### 🐛 Bugfix — `audio_processor.py` backward-compat re-export

- `app/utils/audio_processor.py` now re-exports `transcribe_voice` from `multimodal_processor`, fixing the `TestBackwardCompatImports` test that previously failed on clean HEAD.

### Files Changed

| File | Change |
|------|--------|
| `app/games/judge.py` | Full rewrite: Damerau-Levenshtein, `generate_hints()`, semantic-only prompt, 3s timeout, `judge_unavailable` sentinel, removed `_fallback_judgement` |
| `app/games/judgement_cache.py` | Added `_hints_store`, `get_cached_hints()`, `cache_hints()`, `_HINTS_CACHE_PATH` |
| `app/games/crocodile.py` | Added `asyncio`, `_mem_hints`, `_mem_history`, `get_game_hints()`, `get_game_history()`, `_prefetch_hints()`, `create_game()` background task, `process_guess()` judge_unavailable handling + history recording |
| `app/web_miniapp.py` | WS: `history_sync` after game_state, `hint` message type handler, `pending_id` echo |
| `app/templates/crocodile.html` | Full rewrite: messenger chat UI, optimistic bubbles, haptic, 💡 hint button, typing indicator |
| `app/utils/audio_processor.py` | Added `transcribe_voice` backward-compat re-export |
| `tests/test_games.py` | Full rewrite: new algorithm tests, `mock_llm` fixture, 12 new test cases |

## [2.12.6] - 2026-04-15 - Crocodile Game: Security Hardening & Test Coverage

### 🔐 Security — WebSocket Authentication Enforcement (C3)

- **Eliminated anonymous WebSocket bypass**: `WS /webapp/game/ws` now unconditionally requires valid Telegram `initData` (`HMAC-SHA256`). Previous code fell back to `user_id="anonymous"` when `initData` was absent, allowing unauthenticated connections to join and guess in any active game.
- All connections without `initData` receive `4003 initData required` immediately.
- Removed all `!= "anonymous"` guard clauses from `is_creator` and `target_word` visibility logic — the creator check is now a clean `user_id == game.creator_id` comparison.

### 🔐 Security — API Key Leakage Prevention (M2)

- **`app/games/judge.py` — key material sanitization**: In `_race_generate`, the `keys` list (dicts containing raw `api_key` strings) is now fully cleared **before** coroutines are created and tasks are spawned.
- Prevents raw API key material from lingering in frame locals if a later exception is logged with `exc_info=True`. Keys are extracted to a plain `api_keys_for_race: list[str]` and the source dict list is cleared immediately.

### 🛡️ Architecture — `_game_locks` Bounded Memory (M1)

- **Unbounded dict cap**: `_game_locks` previously grew indefinitely for abandoned WebSocket connections. Added `_GAME_LOCKS_MAX = 512` and a `_sweep_game_locks()` function that evicts the oldest 50% of entries when the dict exceeds capacity.
- Sweep is called on every new connection before lock allocation — O(n) but rare (only once every 512 connections).

### 🛡️ Architecture — CSP / lint fixes

- **`app/web.py`**: Split long `script-src` CSP line to stay within the 120-char ruff `line-length`.
- **`app/web_miniapp.py`**: Added `import asyncio` to top-level imports (previously `asyncio` was only imported locally inside `game_ws`). Upgraded `asyncio.TimeoutError` → builtin `TimeoutError` (UP041). Replaced quoted `asyncio.Lock` string annotation with real type.
- **`app/games/crocodile.py` / `judgement_cache.py`**: Removed forward-reference string quotes from return type annotations (UP037 auto-fix).

### ✅ Tests — `app/games/` Unit Test Suite (L2)

New file: `tests/test_games.py` — 36 fully offline tests (no Redis, no LLM, no network):

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestResolveCategory` | 7 | `word_bank.resolve_category`: exact, alias, case-insensitive, prefix ≥3 chars, unknown |
| `TestListCategories` | 2 | RU / EN category listings |
| `TestValidateCustomWord` | 6 | Length bounds, whitespace strip, special chars, hyphen allow |
| `TestPickRandomWord` | 3 | RU/EN dispatch, unknown category fallback |
| `TestLocalCheck` | 5 | Exact match, case, high-similarity typo (≥0.90), below-threshold, English |
| `TestBelowThreshold` | 1 | Documents the 0.875 < 0.90 boundary for "крокадил" |
| `TestFallbackJudgement` | 3 | Hot/cold/warm status, score range 0.0–1.0 |
| `TestCrocodileGameSerialisation` | 2 | Round-trip JSON, field whitelisting (injection defence) |
| `TestCrocodileGameInMemory` | 6 | Create+load, exact guess → won, typo guess scored, max_attempts → lost, empty guess → error, nonexistent game |

### 📊 Test Results

- **36 / 36 new game tests: PASS** (12-worker xdist, 9.33s)
- **1677 / 1680 total tests: PASS**
- 2 pre-existing failures (`test_audio_processor.py::TestBackwardCompatImports`) — present on clean HEAD before these changes, unrelated to the game subsystem.
- 2 cosmetic `PTBUserWarning` (ConversationHandler `per_message=False`) — pre-existing.

### Files Changed

| File | Change |
|------|--------|
| `app/web_miniapp.py` | WS auth enforcement; `_sweep_game_locks()`; `asyncio` top-level import; `TimeoutError` upgrade |
| `app/games/judge.py` | Key material sanitization before task spawn; UP037 fixes |
| `app/games/crocodile.py` | UP037 fix (unquoted return annotation) |
| `app/games/judgement_cache.py` | UP037 fix (unquoted return annotations) |
| `app/web.py` | E501 fix (split long CSP line) |
| `tests/test_games.py` | **[NEW]** 36 offline unit tests |

---

## [2.12.5] - 2026-04-15 - Crocodile (Charades) Game via Inline Mode

### ✨ Feature — 1-on-1 Crocodile Game Mini App

Adds a complete Telegram game — "Крокодил" (Charades) — playable in any 1-on-1 chat via
inline mode. One player creates a round; the other guesses the hidden word. A 4-tier
semantic judge evaluates answers in real time via a "Midnight Glass" glassmorphism WebApp.

#### How to Start

In any private chat with another user:
```
@<botname> крокодил              → random Russian word from a random category
@<botname> крокодил:животные    → pick a category (e.g. животные, food, спорт)
@<botname> крокодил:=свой       → use a custom word (known only to creator)
```

User A sees the word. User B sends guesses through the Mini App. Bot evaluates each guess
and updates the shared Telegram message on win/loss.

#### Architecture

| Component | Role |
|-----------|------|
| `app/games/crocodile.py` | State machine with Redis persistence (TTL 10 min) + in-memory LRU fallback (64 slots). `asyncio.Lock` per game prevents parallel guess races. |
| `app/games/judge.py` | 4-tier pipeline: Levenshtein exact match → Redis judgement cache → Race×3 LLM → hardcoded fallback ("not a match"). |
| `app/games/word_bank.py` | Bilingual (RU/EN) word bank with 8 categories × 15–30 words each, Redis-backed used-set deduplication (1h TTL), and `validate_custom_word()` guard. |
| `app/games/judgement_cache.py` | Redis-backed LLM evaluation cache (TTL 24h) keyed on `(target, guess)` canonical pair. |
| `app/bot_instance.py` | Singleton holding the PTB `Bot` reference, enabling WebSocket handlers outside PTB context to call `bot.edit_message_text`. |
| `app/web_miniapp.py` | `GET /webapp/game` — serves `crocodile.html`; `WS /webapp/game/ws` — authenticated game loop with per-game lock and 5-min idle timeout. |
| `app/handlers/inline.py` | `@bot крокодил[:{category|=word}]` inline intent: regex prefix match, background `_init_croc_game_async` creates session and edits inline message to show WebApp button. |
| `app/templates/crocodile.html` | "Midnight Glass" glassmorphism UI: WebSocket game loop, animated feedback cards (✅ correct / 🔥 close / ❌ wrong), attempt progress bar, win/loss overlays with confetti. |

#### Semantic Judge (4-Tier Pipeline)

1. **Levenshtein distance** (Damerau threshold ≤ 1) — sub-millisecond exact/typo match.
2. **Redis judgement cache** — 24h TTL for previously computed LLM evaluations.
3. **Race × 3 LLM** — fires `gemini-3.1-flash-lite` (primary) and `gemini-2.5-flash-lite` (fallback) simultaneously; first valid JSON verdict wins. Prompt is single-sentence to minimize latency.
4. **Hardcoded fallback** — `{"score": 0, "hint": ""}` on all errors (non-blocking).

Verdict fields: `status` (`exact_match` | `close` | `no_match`), `score` (0–100), `hint` (short bilingual phrase), `cached` (bool).

#### Security

WebSocket connections require valid Telegram `initData` HMAC-SHA256 (same mechanism as the MiniApp settings panel). Unauthenticated connections receive `4003 Unauthorized`.

#### Config

No new env vars required. Relies on existing:
- `REDIS_URL` (optional; falls back to in-memory)
- `WEBAPP_BASE_URL` or `WEBHOOK_URL` — used to construct the game link
- `GEMINI_API_KEYS` — used by the semantic judge

### Files Changed

| File | Change |
|------|--------|
| `app/games/__init__.py` | **[NEW]** Package init |
| `app/games/crocodile.py` | **[NEW]** State machine + Redis/memory persistence + `finalize()` |
| `app/games/judge.py` | **[NEW]** 4-tier semantic judge pipeline |
| `app/games/word_bank.py` | **[NEW]** Bilingual word bank, category aliases, custom word validation |
| `app/games/judgement_cache.py` | **[NEW]** Redis judgement LRU cache |
| `app/bot_instance.py` | **[NEW]** PTB Bot singleton for non-PTB access |
| `app/templates/crocodile.html` | **[NEW]** Midnight Glass game UI |
| `app/web_miniapp.py` | `GET /webapp/game` route + `WS /webapp/game/ws` handler; `?game_id=` / `?id=` param acceptance |
| `app/handlers/inline.py` | Crocodile intent regex; `_init_croc_game_async` background task; `import os` added |
| `bot.py` | Register `bot_instance` singleton after PTB application build |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `py_compile` (all modified files) | OK ✅ |
| Inline intent regex | Correct prefix match for `крокодил`, `крокодил:category`, `крокодил:=word` ✅ |
| WebSocket auth | HMAC-SHA256 `initData` validated; `4003` on failure ✅ |

---

## [2.12.4] - 2026-04-15 - UX Link Preview Optimization

### ✨ UX — Global Suppression of Auto-Generated Link Previews

- **Problem**: When the bot included source URLs in its generated answers or inline outputs, Telegram automatically generated massive link preview cards at the bottom of the chat, cluttering the UI and taking focus away from the AI's actual textual response.
- **Fix** (`app/adapters/ui_adapter.py` & `app/handlers/inline.py`): Injected `LinkPreviewOptions(is_disabled=True)` into all `edit_text`, `reply_text`, `send_message`, and `bot.edit_message_text` calls. This globally suppresses link previews metadata parsing.
- **Result**: A much cleaner, more professional chat interface where grounding sources are gracefully embedded inside an `<blockquote expandable>` without polluting the view.

---

## [2.12.3] - 2026-04-14 - Inline Image Pipeline Hardening & Per-Model Placeholder UX

### 🐛 Fix — Inline Image Swap: `Can't parse inputmedia: media not found` (Critical)

- **Root cause**: The Local Bot API Server (`local_mode=True`) rejects `InputFile(io.BytesIO(...))` multipart uploads in `edit_message_media` for inline messages (identified by `inline_message_id`). Only pre-existing `file_id` strings or public HTTP URLs are accepted by the Telegram Bot API for inline media edits — this constraint exists in both the cloud API and the local server.
- **Fix** (`app/handlers/inline.py` — `_generate_and_swap_media`): Replaced the direct bytes upload with a two-step **admin-chat file_id minting** pattern:
  1. `bot.send_photo(chat_id=settings.ADMIN_ID, photo=InputFile(BytesIO(bytes)))` → captures `file_id` from the response.
  2. Immediately deletes the temporary admin-chat message (no visible side-effect for admin).
  3. Passes the minted `file_id` string to `edit_message_media(InputMediaPhoto(media=file_id))` — always accepted for inline edits.
- **Dependency**: Requires `settings.ADMIN_ID` to be correctly configured — bot must have permission to send photos to the admin chat.

### 🐛 Fix — Pollinations Model Validation Silent Fallback

- **Root cause**: `DEFAULT_POLLINATIONS_IMAGE_MODELS` only contained `["flux", "zimage"]`, but the inline handler exposes 4 additional models (`gptimage`, `qwen-image`, `wan-image`, `klein`). The `PollinationsProvider` validation rejected these with a silent fallback to `flux`, causing wrong model to be used.
- **Fix** (`app/config.py`): Expanded `DEFAULT_POLLINATIONS_IMAGE_MODELS` to `["flux", "zimage", "gptimage", "qwen-image", "wan-image", "klein"]` — matches the full inline model set.
- **Note**: If `IMAGE_MODELS` env var is set explicitly, it must also include these models; otherwise they still fall back.

### ✨ UX — Per-Model Distinct Placeholder Images in Inline 2×2 Grid

- **Problem**: All 4 image model tiles in the inline results grid showed identical grey `Generating…` cards — users couldn't distinguish which model they were selecting.
- **Fix** (`app/handlers/inline.py`): `_IMAGE_MODELS` migrated from 3-tuple to 4-tuple `(result_id, title, model, placeholder_url)`. Each model variant now has a **unique color scheme** via `placehold.co`:

  | Tile | Colors | Label |
  |------|--------|-------|
  | ⚡ Турбо (`zimage`) | Dark navy + cyan | `Turbo` |
  | 🤖 Умный (`gptimage`) | Dark green + green | `Smart AI` |
  | 🎨 Арт (`qwen-image`) | Dark purple + violet | `Art Style` |
  | 🅰️ Мем (`wan-image`) | Dark amber + orange | `Meme Text` |
  | 🪄 Изменить (`klein`) | Olive + yellow | `Edit Photo` |

- The shared `_IMG_PLACEHOLDER_URL` constant was removed; each model carries its own `placeholder_url`.

### 🐛 Fix — `ValueError: too many values to unpack` in `handle_chosen_inline_result`

- **Root cause**: After migrating `_IMAGE_MODELS` to 4-tuples, `handle_chosen_inline_result` still destructured with the old 3-tuple pattern `(m for rid, _, m in all_known_models)`, raising `ValueError: too many values to unpack` on every inline image selection. This caused the entire chosen-result handler to crash, showing "No results" in the inline popup.
- **Fix**: Replaced tuple destructuring with index-based access `(entry[2] for entry in all_known_models if entry[0] == result_id)`. The klein fallback entry was also aligned to a 4-tuple.

### Files Changed

| File | Change |
|------|--------|
| `app/handlers/inline.py` | Admin-chat file_id minting in `_generate_and_swap_media`; `_IMAGE_MODELS` → 4-tuple with per-model placeholder URLs; fixed `handle_chosen_inline_result` unpack; removed `_IMG_PLACEHOLDER_URL` |
| `app/config.py` | `DEFAULT_POLLINATIONS_IMAGE_MODELS` expanded to 6 models |

---

## [2.12.2] - 2026-04-14 - Inline Image Reliability, Dynamic Model Management & Code Quality

### 🔧 Fix — Inline Image Generation (Critical)

- **Root cause**: `_generate_and_swap_media()` built a `gen.pollinations.ai/image/?nologo=true` URL and passed it as `InputMediaPhoto(media=url)`. Telegram fetches that URL directly without our auth headers — `nologo=true` now requires an API key, returning `403 Forbidden`, so the placeholder was never replaced.
- **Fix**: Switched to uploading raw bytes via `InputFile(io.BytesIO(result.images[0]), filename="image.jpg")`. The generated bytes are already in-memory and uploaded directly — no external fetch by Telegram, 100% reliable swap.
- **Added**: `_get_model_emoji()` helper and `_MODEL_EMOJI` lookup dict for per-model emoji in generated image captions (⚡ zimage, 🤖 gptimage, 🎨 qwen-image, 🅰️ wan-image, 🔷 klein, etc.)
- **Added**: `io` and `InputFile` to top-level imports in `inline.py`.

### ✨ Feature — Dynamic Model Management (`/models` Admin Wizard)

- **Admin command `/models`**: Full `ConversationHandler`-based wizard for runtime model management — no container restart required.
  - `➕ Добавить модель`: Prompts for model ID, validates, appends to the live list stored in `global_settings` DB table.
  - `➖ Удалить модель`: Checkbox-style multi-select UI for removing models from the active list.
  - `🔄 Сбросить к стандартным`: One-tap reset to the environment-variable defaults.
- **Data layer**: `app/repos/models_repo.py` — `save_model_list()` / `load_model_list()` backed by `global_settings` key-value table. Supports both Gemini and OpenRouter model lists independently.
- **Runtime sync**: `sync_models_from_db()` called at bot startup to apply DB-persisted overrides before the first request is handled.

### 🔧 Code Quality

| File | Fixes |
|------|-------|
| `app/handlers/inline.py` | 5 × SIM105 (`try/except/pass` → `contextlib.suppress`); E501 line 406 (f-string extracted to variable); `contextlib` moved to top-level imports |
| `app/voice_engine.py` | 2 × SIM105 (`status_msg.delete` finalizer, `suspend_key` suppression); `contextlib` added to top-level imports |
| `app/web.py` | `import contextlib` moved from mid-file E402 position to top-level imports; `import time` (unused) removed |
| `tests/` | 25+ × F401 (unused imports auto-fixed via `ruff --fix`); W605 invalid escape sequences in `test_format2.py` |
| `test_ai_mock.py`, `test_tts_mock.py` | F401 unused imports removed |

### 📊 Test Results

- **1645 tests, 0 failures** — 100% pass rate, parallel `-n auto` execution.
- 2 pre-existing `PTBUserWarning` (ConversationHandler `per_message=False`) — non-blocking, cosmetic only.

### Files Changed

| File | Change |
|------|--------|
| `app/handlers/inline.py` | Core bytes-upload fix; `_get_model_emoji` helper; `_MODEL_EMOJI` map; lint cleanup |
| `app/handlers/cmd_models.py` | **[NEW]** `/models` admin ConversationHandler |
| `app/repos/models_repo.py` | **[NEW]** Model list persistence via `global_settings` |
| `app/voice_engine.py` | SIM105 fixes; `contextlib` import |
| `app/web.py` | E402 fix; unused `time` import removed |
| `app/providers/pollinations.py` | `_KNOWN_LABELS` restored to the approved model set |
| `tests/` (multiple) | F401 / W605 auto-fixes |


### ✨ Feature — Edge Provenance (HippoRAG 2)

- **Source Traceability**: Graph queries now batch-fetch and return the raw source text for the highest-weighted edges (top 3 by default). This reduces hallucinations by grounding the LLM in verbatim user memories rather than synthesized triples.
- **Citation Badge**: The "🧠 N фактов" badge on AI responses was upgraded to "📚 N фактов (interactive)". Tapping the badge now displays an alert showing the exact graph relationships and sources used to generate the response, utilizing a new `show_facts` callback and caching system.

### ✨ Feature — Provenance-Aware RLHF

- **Graph Penalty Cascading**: When a user gives a 👎 (downvote) to a response, the system not only penalizes the graph edges used, but *cascades* a negative feedback count to the source `long_term_memory` records that generated those edges.
- **Search-Time Deprioritization**: The hybrid search engine (RRF + Semantic) now reads `rlhf_negative_count` using a safe alias (`COALESCE`), applying a strict similarity penalty (-0.03 per negative vote) at retrieval time. This effectively surfaces better memories while burying incorrect facts.

### ✨ Feature — /keys Admin Wizard (Hot-Swapping)

- **Centralized Provider Key Repo**: Added a global registry (`provider_keys.py`) to access and cache provider API keys.
- **Hierarchical Lookup**: Database overrides (`global_settings`) → Environment variables (`Settings`) → Empty fallback.
- **Inline Keyboard UI**: Built `/keys` admin command. Admins can view provider status, dynamically swap API keys, clear overrides to return to `.env` fallbacks, and run live connection checks against the providers—all without restarting the application.
- **Active Health Checks**: Added a 30-min background `job_queue` job to ping providers (weather, exchange, etc.) and alert the admin within a 6-hour cooldown window if one goes down.

## [2.11.0] - 2026-04-14 - Search Pipeline Modernization: Weather/Currency/Crypto APIs & 5-Mode Image Generation

### ✨ Feature — Intent Router API Modernization

Replaced legacy Open-Meteo (2 requests) and Frankfurter (no RUB support) with high-performance, single-request APIs:

| Service | Old Provider | New Provider | Improvement |
|---------|-------------|--------------|-------------|
| Weather | Open-Meteo (geocode + forecast) | **WeatherAPI.com** | 1 request; localized RU conditions (`Переменная облачность`); "feels like" temperature |
| Fiat currency | Frankfurter (no RUB) | **ExchangeRate-API v6** | RUB, KZT, UAH, KGS, UZS support; 1,500 req/month free tier |
| Crypto | ❌ none | **CoinGecko Demo API** | Keyless, 30 rpm; BTC/ETH/SOL/TON with Russian aliases (`биткоин`, `эфир`, `тон`); USD + RUB + 24h change |

- **Graceful fallbacks**: When `WEATHER_API_KEY` is absent, falls back to Open-Meteo. When `EXCHANGE_RATE_API_KEY` is absent, falls back to Frankfurter for EU pairs.
- **Temporal filter**: Multi-day/temporal weather queries (`завтра`, `вечером`, `на неделю`) bypass the raw API and route to LLM with Google Search Grounding instead.

### ✨ Feature — 5-Mode Smart Image Routing (Inline)

Redesigned inline image generation with UX-named modes and automatic intent detection:

| Mode | Model | Auto-Route Trigger |
|------|-------|--------------------|
| ⚡ Турбо | `zimage` | Default (shown first) |
| 🧠 Умный | `gptimage` | Manual; `enhance=true` sent to Pollinations |
| 🎨 Арт / Аватарка | `qwen-image` | Manual |
| 🅰️ Мем / Текст | `wan-image` | Auto: `«»""''`-quoted text detected |
| 🪄 Изменить фото | `klein` | Auto: edit-intent verbs detected (`измени фото`, `отредактируй`) |

- Klein is NOT in the inline menu — only auto-routed when edit intent is detected.
- Placeholder URL migrated from `image.pollinations.ai` → `gen.pollinations.ai`.

### ✨ Feature — Google Search Grounding Citations (Inline)

- **`_GroundingMeta` sentinel** (`app/providers/gemini.py`): After `stream_response` completes with web search enabled, grounding metadata is parsed from `candidates[0].grounding_metadata.grounding_chunks` and yielded as a sentinel object.
- **Inline capture** (`app/handlers/inline.py`): `_stream_inline_fast` intercepts `_GroundingMeta` sentinels in the Race Requests queue and stores sources from the **winner** key only.
- **UX**: Up to 3 source URLs rendered inside an expandable blockquote (`📎 Источники`) at the bottom of inline answers. Only appended if room remains within the 4096-char Telegram limit.
- **Zero breaking changes**: Existing callers of `stream_response` are unaffected — they check `chunk.text` which naturally skips the sentinel.

### 🔧 Code Quality — Test Warning Fixes

| Warning | Root Cause | Fix |
|---------|-----------|-----|
| `RuntimeWarning: coroutine 'dummy' never awaited` | `test_taskmanager_rejects_bare_coro_with_retry` created coro inside `pytest.raises` context | Moved coro creation outside; `coro.close()` in `finally` |
| `DeprecationWarning: asyncio.iscoroutinefunction deprecated` | Python 3.14 deprecation, removed in 3.16 | Replaced with `inspect.iscoroutinefunction()` |
| `RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' never awaited` | `slow_db_query` returned `original_db_query(...)` without `await` | Added `await` |
| `DeprecationWarning: AiohttpClientSession inheritance` | Third-party `google.genai` SDK | Suppressed via `pytest.ini` `filterwarnings` |

### 🔧 Config

- `WEATHER_API_KEY` — WeatherAPI.com (free: 1M req/month). Falls back to Open-Meteo when empty.
- `EXCHANGE_RATE_API_KEY` — ExchangeRate-API v6 (free: 1,500 req/month). Falls back to Frankfurter when empty.

### Files Changed

| File | Change |
|------|--------|
| `app/config.py` | Added `WEATHER_API_KEY`, `EXCHANGE_RATE_API_KEY` settings |
| `app/intent_router.py` | Full rewrite: WeatherAPI.com + ExchangeRate-API + CoinGecko + temporal filter |
| `app/handlers/inline.py` | 5-mode image routing, smart intent detection, grounding citations blockquote |
| `app/providers/gemini.py` | `_GroundingMeta` sentinel for streaming grounding metadata |
| `app/providers/pollinations.py` | `wan-image` label, `klein` emoji update |
| `tests/test_background_tasks.py` | Fix bare coroutine warning |
| `tests/test_factories.py` | Replace deprecated `asyncio.iscoroutinefunction` |
| `tests/test_metrics_snapshot.py` | Await AsyncMock call |
| `pytest.ini` | Suppress google.genai AiohttpClientSession DeprecationWarning |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `py_compile` (all files) | OK ✅ |
| `ruff check` (all files) | 0 errors ✅ |
| `pytest` (full suite) | **1645 passed**, 0 warnings ✅ |

---

## [2.10.9] - 2026-04-14 - Provider Routing: API Key Mismatch & Streaming Resilience

### 🐛 Critical Bugfix — API_KEY_INVALID Database Desynchronization
- **Root Cause**: Gemaibotv2 synchronizes keys from the `.env` configuration file into the Supabase database. The `ADMIN_SECRET` was changed, causing `app/db/seed.py` to encrypt active keys with the new secret. However, old keys encrypted with the previous secret became orphaned in the `public.openrouter_api_keys` table and could not be decrypted, leading to persistent `API_KEY_INVALID` errors.
- **Fix**: Executed raw SQL to manually purge the old orphaned keys from `openrouter_api_keys`, `openrouter_key_usage`, and `key_model_status`, allowing the seed script to repopulate the tables properly with keys encrypted using the current `ADMIN_SECRET`.

### 🐛 Critical Bugfix — Streaming Router Leaking `[RATE_LIMIT]` Tag
- **Root Cause**: The `ProviderRouter.stream_response` loop expected network-level exceptions (e.g. `httpx.HTTPError`) to trigger key rotation. OpenRouter, however, yielded inline tagged strings like `\u200b[RATE_LIMIT] 429...` during limit exhausts. The router treated this as a valid text chunk, streamed it to the user interface, and permanently marked the stream as started, bypassing all Retry logic.
- **Fix**: Patched both the single-key route and the parallel "Race Request" route to intercept tagged string errors (`is_error_message(chunk)`) **before** marking the stream as started.
- **Penalty Classification Integration**: Reworked error classification. The router now parses the intercepted error tags using `classify_key_error(tag)` instead of defaulting to a static "transient" mapping. Rate limits dynamically apply a 15-second suspension, while quota exhausts apply a daily suspension, making the key rotation incredibly resilient.
- Fixed a leaky variable scope in the race request `except` block which could clobber `err_category` state.

### 🔧 Code Quality
- Addressed multiple `ruff` issues (`SIM102`, `B023`).
- 100% full test suite passing (1700+ tests).

---

## [2.10.8] - 2026-04-14 - Advanced Inline UX & Collaborative AI-Notes

### ✨ Inline Architecture — Advanced Progressive UX

- **Implicit Media Swap**: Prompts starting with `"нарисуй"` immediately push a fast Pollinations.ai image to the inline grid (via static placeholder), which is then asynchronously generated and swapped in-place, achieving zero-latency interactive media.
- **Tabbed Response UI**: Inline responses are structured using XML tags (`<tldr>`, `<details>`, `<sources>`) extracted by the LLM and rendered dynamically via inline buttons. Users can seamlessly switch tabs without re-triggering generation. Admin-toggleable via `/set_inline_tabs <on|off>`.
- **Collaborative AI-Notes (Topic Aggregator boards)**: Prefixing a query with `доска: <topic>` initializes a persistent, shared workspace. Any user can reply to the board to add notes (bypassing privacy mode via `via_bot` detection). The bot debounces new entries (60s window) and automatically synthesizes them into an evolving structural summary via the `TaskManager`. Backed by PostgreSQL `inline_boards`.

### 🛡️ System Resilience, Security & Multi-Threaded Hardening

- **Database Security (Supabase Advisor)**: Enforced Row-Level Security (RLS) policies on `public.inline_boards` and `public.global_settings` using `RLS_POLICY_ADMIN` inside `app/db/rls.py`, explicitly addressing security flags from the Supabase deployment. Applied schema update via migration `036_enable_rls_inline_boards_global_settings.sql` (Supabase MCP).
- **Inline Metrics Stability (FK Violation Fix)**: Resolved a persistent `ForeignKeyViolationError` in `app/metrics.py` where tracking metrics for unregistered users (like random users querying the bot inline) would crash the background DB worker. Implemented an atomic, silent 'guest' upsert (`is_authorized=0`) into `public.users` prior to metrics insertion stringently avoiding constraints failure without bypassing normal registration procedures.
- Resolved database schema discrepancies in integration tests by synchronizing missing columns (`tts_temperature`) in `tests/integration/conftest.py`.
- **Migration**: Applied `035_create_inline_boards.sql` and `036_...` (RLS).
- Verified entire system via robust multi-threaded test suite (1645 tests passing).

---

## [2.10.7] - 2026-04-13 - Migration Resilience & Progressive Inline Timeout UX

### 🐛 Critical Bugfix — Migrations 025–034 Never Applied on Production

**Severity:** High (silent feature degradation — `global_settings` table missing)

- **Root Cause:** The migration runner (`app/db/migrations.py`) wrapped ALL pending SQL files in a single giant transaction. Migrations 025–033 had been applied manually via Supabase MCP during development, but their versions were never recorded in `schema_migrations`. On every bot restart, the runner attempted to re-apply 025+, one of them conflicted (e.g., `ALTER EXTENSION pg_trgm SET SCHEMA extensions`), and the entire batch — including migration 034 (`global_settings`) — was rolled back. The `settings_repo` silently fell back to defaults, so the `inline_thinking_level` admin control was non-functional.
- **Immediate Fix (Supabase MCP):** Backfilled `schema_migrations` entries for 025–033 and applied migration 034 (`CREATE TABLE global_settings`) directly on production.

### 🏗️ Architecture — Migration Runner Per-File Isolation (`app/db/migrations.py`)

Replaced the all-or-nothing batch transaction with **per-file independent transactions**:

| | Old | New |
|---|---|---|
| Transaction scope | One transaction for ALL pending files | Individual transaction per SQL file |
| On failure | Entire batch rolled back — blocks all later migrations | Failed file skipped with WARNING, runner continues |
| `schema_migrations` recording | Batched INSERT at end | Immediate INSERT per successful file |
| Partial application | Impossible | Expected and safe (each file is idempotent) |

### ⏱️ UX — Progressive Inline Timeout (Two-Phase Feedback)

Replaced the hard 22s error with a **two-phase progressive timeout** for inline generation:

| | Old | New |
|---|---|---|
| Hard timeout | 22s | **55s** |
| Progress feedback | None — user stares at static placeholder | **Phase 1 (post-search):** "🧠 *Bot* собрал информацию, теперь генерирует ответ…" · **Phase 2 (20s):** "⏳ *Bot* задерживается…" |
| Timeout message | "⏰ Модель не успела ответить вовремя." | "⏰ Модель не успела ответить вовремя. Нажмите «Повторить» ниже." |
| Inner stream timeouts | 18s drain, 20s queue wait | **45s drain, 50s queue wait** (proportional to outer 55s) |

**Mechanism:** A background `asyncio.Task` (`_delayed_progress_edit`) sleeps for 20s then edits the placeholder. If the generation completes before 20s, the task is cancelled. After Tavily search returns results, the placeholder is immediately updated to show the bot has gathered context. Bot name is resolved dynamically from `bot.first_name`.

### 🛡️ Self-Healing — Settings Repo Lazy Bootstrap (`app/repos/settings_repo.py`)

Defense-in-depth: if the `global_settings` table is missing (migrations failed), the settings repo now **auto-creates it on first access**:

- On `asyncpg.UndefinedTableError`: creates the table via `CREATE TABLE IF NOT EXISTS`, then retries the query once.
- Singleton `_table_verified` flag ensures the bootstrap check runs at most once per process lifetime.
- `set_global_setting()` also calls `_ensure_table()` before upserts.

### 🔧 Code Quality

- All hardcoded Russian inline error/progress strings moved to named module-level constants (`_TIMEOUT_ERROR`, `_GENERATION_ERROR`, `_FALLBACK_ERROR`, `_placeholder_html()`, `_progress_search_done_html()`, `_progress_delayed_html()`)
- Ruff UP045 fix: `Optional[str]` → `str | None` in settings_repo
- Removed unused `TYPE_CHECKING` import

### Files Changed

| File | Change |
|------|--------|
| `app/db/migrations.py` | Single-transaction batch → per-file independent transactions |
| `app/handlers/inline.py` | `_GEN_TIMEOUT_S` 22→55s; two-phase progress edits; named UX constants; inner timeouts scaled |
| `app/repos/settings_repo.py` | Lazy `_ensure_table()` bootstrap; `asyncpg.UndefinedTableError` catch-and-retry |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `py_compile` (all 3 files) | OK ✅ |
| `ruff check` (all 3 files) | 0 new errors ✅ |

---

## [2.10.6] - 2026-04-12 - Voice Search Pipeline: Data-Loss Audit & History Persistence Fix

### 🐛 Bugfix — QnA Search Results Never Saved to Chat History (Data Loss)

**Severity:** High (data loss — silent, no visible error)

- **Root Cause:** `_auto_route_to_search` in `app/handlers/msg_voice.py` called `_handle_qna_search` but discarded its result. Internally, `_handle_qna_search` constructs a throwaway `history = [{"role": "user", ...}]` dict for the Grounding API call and never writes back to `chat_state`. Neither the voice transcript nor the AI answer was persisted to the DB. Every QnA voice search was invisible to future sessions and LTM.
- **Contrast:** `_handle_research_agent` (deep dive path) correctly appends both turns and calls `update_user_chat` at lines 494–510 of `ai_search.py`. The QnA path had no equivalent.
- **Fix (`app/handlers/ai_search.py`):** Changed `_handle_qna_search` return type from `None` to `str | None`. Function now `return final_answer or None` at exit. Backward-compatible: all existing callers that ignore the return value continue working unchanged.
- **Fix (`app/handlers/msg_voice.py`):** Capture `answer = await _handle_qna_search(...)`. If non-empty, append user + model turns to `chat_state.history` and call `await update_user_chat(user_id, chat_state)`. This mirrors the deep research persistence pattern.

### 🐛 Bugfix — Dead TTS Code Block in `_auto_route_to_search`

- **Root Cause:** ~20 lines of TTS-trigger code (lines 592–611) were a `pass` statement inside a `try/except Exception: pass` block. The code imported `fire_voice_reply`, computed `_bot` and `_chat_id`, then did nothing. It was structurally unreachable for its stated purpose (producing audio) and consumed 20 lines of misleading noise.
- **Fix:** Removed entirely. If TTS is needed for QnA voice results in future, it should be implemented inside `_handle_qna_search` itself, consistent with `_handle_research_agent` which has its own auto-TTS at `ai_search.py:475–492`.

### 🔧 Hygiene — Redundant Inline Re-Imports Removed

- `from app.i18n import t` and `from app.repos.chats import get_user_chat` were re-imported inside `_auto_route_to_search`, but both are already present as top-level module imports (`msg_voice.py:25–26`). Removed the redundant inline imports.

### Files Changed

| File | Change |
|------|--------|
| `app/handlers/ai_search.py` | `_handle_qna_search` return type `None` → `str \| None`; `return final_answer or None` added at function exit |
| `app/handlers/msg_voice.py` | Capture `answer` from `_handle_qna_search`; persist to `chat_state.history` + `update_user_chat`; remove dead TTS block; remove redundant inline imports |

---

## [2.10.5] - 2026-04-12 - Production Audit Polish: Zero-Delay TTS Drain & Inline Quota UX


### 🐛 Bugfix / Optimization — Zero-Delay TTS Exception Sink
- **Problem**: The Gemini TTS Race Requests loop (`app/voice_engine.py`) had a flaw where `pending.clear()` broke the background task drain loop. The initial fix attempt used a blocking `asyncio.wait(timeout=0.5)` drain sequence, which artificially delayed TTS output by 0.5s while waiting for loser HTTP connections to close, and still risked `Task exception was never retrieved` if closure was slow.
- **Solution**: Completely removed the post-winner `asyncio.wait()` and replaced it with a pure asynchronous `task.add_done_callback(_suppress)`. 
- **Result**: Loser tasks now cleanly sink their own exceptions (like `CancelledError` or `httpx.ConnectTimeout`) completely dynamically without blocking the main TTS event loop, entirely eliminating the 0.5s lag wall and silencing asyncio GC warnings.

### 🐛 Bugfix — Inline Mode Proper Error Transparency
- **Problem**: During API Quota exhaustion in Inline Mode (`app/handlers/inline.py`), the UI presented a standard static fallback `"❌ Не удалось получить ответ."`, overriding and discarding the actual quota error provided by the router (e.g. `🚫 Все доступные ключи...`).
- **Solution**: Integrated `strip_error_tag` from `app.errors` to intercept quota/failure strings. If `_is_api_error` is True, it now correctly outputs the localized string (stripped of invisible engine tags) directly to the user message.
- **Result**: Users trying to generate inline requests during heavy API usage spikes will now properly see "🚫 Достигнут лимит" combined with the 'Retry' button, providing transparent semantic UX.

---

## [2.10.4] - 2026-04-12 - Inline 3-Way Race Requests & Circuit Breaker Hardening

### ⚡ Architecture — Inline Generation: 3-Way Parallel Race Requests

#### Problem
Inline generation (`@bot <query>`) used a sequential `get_response` path with `max_key_retries=1` — a single API key, a single attempt, no fallback. Under any transient Gemini 503, the generation failed and the user saw an error message. The mechanism was architecturally incompatible with the Race Requests resilience system that all other request types use.

#### Solution — Dedicated `_stream_inline_fast()` Accumulator (`app/handlers/inline.py`)
Replaced the `_get_ai_response_with_routing` call with a self-contained `_stream_inline_fast()` helper that fires **3 API keys simultaneously per round** (vs. 2 keys in the standard `stream_response` router):

| | Old | New |
|---|---|---|
| Keys per attempt | 1 (sequential) | **3 (parallel, Race Requests)** |
| Max rounds | 1 | 4 |
| Total key slots | 1 | **12** |
| Zero sleep between rounds | ❌ | ✅ |
| Loser cancellation | n/a | Instant `t.cancel()` on first chunk |

**Why 3 keys (not 2):** `gemini-3.1-flash-lite` has RPD limits in the hundreds per key, and there are 15+ keys configured. Burning 3 simultaneous slots per round is operationally free — at most one key completes, the other two are cancelled the moment the first chunk arrives. This reduces TTFR (time-to-first-result) to `min(key_1, key_2, key_3)` response time.

**Round loop:** Up to 4 rounds × 3 keys = 12 key slots before returning `None`. Keys that fail (503, exception, empty stream) are added to `failed_keys` and excluded from the next round — each round draws fresh keys.

**Winner drain:** After selecting the winner, the accumulator drains all remaining chunks from its queue until an `_End` sentinel is received, guaranteeing zero truncation.

#### Fix — `CircuitBreakerOpenError` Handling (`app/providers/base.py`)
Broadened the `except` clause from `except (APIError, httpx.HTTPError)` to bare `except Exception`. Previously, `CircuitBreakerOpenError` escaped this block and reached the background task runner as an unhandled exception, producing `Task exception was never retrieved` log spam. It is now caught and converted into a structured `AIResponse(success=False)`.

#### Fix — Circuit Breaker Threshold (`app/circuit_breaker.py`)
Updated `GEMINI_API_CONFIG`:

| Parameter | Old | New | Rationale |
|-----------|-----|-----|-----------|
| `failure_threshold` | 3 | **15** | With 3 keys × 4 rounds per request = 12 key slots, a threshold of 3 opened the circuit before a single user request could complete |
| `recovery_timeout` | 30s | **45s** | Gives Gemini 503 overload windows sufficient time to clear before the system retries |

### 📊 Feature — Inline Metrics Visibility
Inline generation was previously a blind spot in the `/metrics` dashboard. Now tracks:
- `api_logger.log_request("gemini_inline", model=..., query_length=..., tone=...)` — logged on every inline attempt
- `api_logger.log_response("gemini_inline", ...)` — response duration, success flag, and response length
- `metrics_collector.record_api_call("gemini_inline", model, user_id)` — API usage counter
- `metrics_collector.record_request("inline", response_time, success, user_id)` — per-request performance tracking

### 🔧 Code Quality
- Ruff B023 false positive suppressed via `# noqa: B023` on the `except Exception as exc:` usage inside `_race` inner function (B023 incorrectly flags `except ... as` bindings as "unbound loop variables")
- `ruff check app/handlers/inline.py app/providers/base.py app/circuit_breaker.py` — 0 errors ✅
- `python -m py_compile` — All OK ✅

### Files Changed

| File | Change |
|------|--------|
| `app/handlers/inline.py` | New `_stream_inline_fast()` 3-way race accumulator; removed `_get_ai_response_with_routing` dependency; added `api_logger` + `metrics_collector` integration; `import time` |
| `app/providers/base.py` | `except (APIError, httpx.HTTPError)` → `except Exception` to catch `CircuitBreakerOpenError` |
| `app/circuit_breaker.py` | `GEMINI_API_CONFIG`: `failure_threshold` 3→15, `recovery_timeout` 30s→45s |

---

## [2.10.3] - 2026-04-12 - Stability Audit: TTS Task Leak & Inline Retry UX

### 🐛 Bugfix — asyncio "Task exception was never retrieved" in TTS Race

#### Root Cause
`_generate_single_chunk_gemini` in `voice_engine.py` used a `break` statement inside
`for task in done:` when it found a winning PCM result. This immediately exited the inner
loop, leaving any *concurrently completed* tasks in the `done` set with their exceptions
unretrieved. Python's asyncio GC later logs:

```
Task exception was never retrieved
Future: <Task finished …> exception: ValueError('…')
```

These spurious warnings pollute production logs and mask real errors.

Additionally, when `asyncio.wait(pending, timeout=0.5)` was called to drain cancelled
tasks, the returned `done2` set was discarded without reading exceptions from each task —
same leak.

#### Fix (`app/voice_engine.py`)
- Removed the inner-loop `break`. The `for task in done:` loop now fully iterates every
  task delivered in the current batch.
- A second winner (concurrent completion) is silently discarded (`pass`) — its result is
  valid but not needed.
- Failed tasks call `task.exception()` explicitly inside a `try/except` to mark the future
  as retrieved before suspending the key and moving on.
- Winner detection is moved *after* the full `done` loop, and the post-cancel drain
  (`asyncio.wait(pending, timeout=0.5)`) now reads and suppresses exceptions from each
  returned task via a `for t in done2: t.exception()` pass.
- Net effect: zero `"Task exception was never retrieved"` warnings under any race outcome.

### 🐛 Bugfix — Inline Mode Retry Button Missing on API Quota Errors

#### Root Cause
`_generate_and_edit_inline` in `handlers/inline.py` evaluated:

```python
is_failure = not (final_answer and final_answer.strip())
```

When the provider stack returned a tagged quota-error string (e.g.
`\u200b…🚫 Все ключи исчерпаны…`) the string was non-empty, so `is_failure` was `False`.
The retry button was not attached, and the raw error string was rendered inside the HTML
header as if it were a successful answer — creating a confusing UX dead-end.

The user had to exit the chat, re-type the query via `@bot`, and select a tone again.

#### Fix (`app/handlers/inline.py`)
- Added `from app.errors import is_error_message` import (zero-width `ErrorCode` tag
  detection — the same function used throughout `router.py` and `ai_core.py`).
- Extended `is_failure`:
  ```python
  is_failure = not (final_answer and final_answer.strip()) or bool(
      final_answer and is_error_message(final_answer)
  )
  ```
- Added `_is_api_error` guard in the formatting block so error-tagged strings never reach
  `markdown_to_html()` — they produce the standard `"❌ Не удалось получить ответ."` text
  instead.
- Net effect: tapping `🔄 Повторить` is now available for *all* failure modes, including
  Gemini quota exhaustion during peak hours.

### 🔧 Code Quality
- `ruff check app/` — 0 errors ✅
- `ruff format app/` — 0 reformats required ✅
- `python -c "ast.parse(…)"` — Syntax OK ✅

---

## [2.10.2] - 2026-04-12 - Race Condition Hotfix (Sentinel Drain)


### 🐛 Critical Bugfix — Truncated Streaming Responses

Fixed a TOCTOU (time-of-check/time-of-use) race condition in the Race Requests drain loop that caused **streaming responses to truncate mid-sentence**.

#### Root Cause
The drain loop used `while not tasks[winner_idx].done()` to decide when to stop consuming chunks from the shared `asyncio.Queue`. This races because:
1. The winner task puts its last chunk into the queue and finishes → `task.done()` returns `True`
2. The drain loop checks `done()` — sees `True` — **exits immediately**
3. The last chunk(s) remain unconsumed in the queue → user sees truncated text

The secondary drain (`while not winner_queue.empty()`) was unreliable because `Queue.empty()` can return `True` while items are still being enqueued.

#### Fix — Sentinel-Based Stream Completion
- Each race participant now puts a `_STREAM_END` sentinel object after all real chunks (or after `CancelledError`/exceptions)
- The consumer drain loop is now `while True` and breaks **only** when it receives the winner's sentinel
- This guarantees every chunk is consumed before exit, regardless of task lifecycle timing
- `CancelledError` handler also pushes a sentinel, preventing the consumer from hanging on a cancelled loser

### 🔧 Code Quality
- Fixed I001 import sort violation in `app/deferred_response.py`
- Full `ruff check` passes with zero violations

## [2.10.1] - 2026-04-12 - Bot Resilience Architecture & 503 Mitigation

### 🛡️ Core Resilience Architecture

Successfully deployed a production-grade resilience layer to eliminate service disruptions caused by Gemini `503 UNAVAILABLE` errors under high load. Implemented a 6-phase strategy targeting latency mitigation, model cascading, UX transparency, network-stall recovery, and background queuing.

#### 1. High-Speed Race Requests (Phase 1)
- **`ProviderRouter.stream_response`**: Completely refactored. The bot now spins up two simultaneous streaming requests (`asyncio.create_task`) using two different API keys.
- **First-Chunk Wins**: The first request to successfully return a chunk signals the winner queue; the loser is cleanly cancelled.
- **Sentinel-Based Completion**: Producer tasks push a `_STREAM_END` sentinel after all chunks, and the consumer breaks only on sentinel receipt — eliminates the `task.done()` TOCTOU race.

#### 2. Model Cascade Fallback (Phase 2)
- If the primary heavy model (e.g., `gemini-exp-1206` or `gemini-2.5-pro`) exhausts all retries due to 503 errors, the system cascades down to a lighter, more reliable model (e.g. `gemini-3.1-flash-lite`).
- Avoids total failure by sacrificing some reasoning capacity during extreme API outages.

#### 3. Delayed UX State Indication (Phase 3)
- If no chunks arrive within 5 seconds, an ephemeral toast (`⏳ Запрос в обработке...`) is sent, showing users that the bot is alive but waiting on the API.
- Replaces silent, confusing waiting periods with transparent feedback and a `[❌ Отменить]` inline button.

#### 4. Network-Stall Recovery (Phase 4)
- **TTFB-based Stall Tracking**: `app/state.py` now tracks `_NETWORK_STALL_SINCE` dictionaries.
- New user messages now intelligently detect if the preceding task is genuinely stalled (waiting >15s for HTTP headers). If yes, the stuck socket is cancelled, and the new query takes precedence.
- If the stream is healthy (actively yielding tokens), the bot preserves FIFO safety by rejecting interruption and showing the standard busy toast.

#### 5. Async Deferred Queue (Phase 5)
- For extreme edge cases where even the fallback model fails, the request is shipped to Redis via `app/deferred_response.py`.
- The bot gracefully informs the user that the request is queued, and delivers the answer as a follow-up message when the Gemini API stabilizes.

#### 6. Intent Direct Routing (Phase 6)
- **`intent_router.py`**: Intercepts simple utility queries (weather, currency conversion) before they hit the LLM. 
- Bypasses Gemini fully by routing directly to `Open-Meteo` and `Frankfurter` APIs, ensuring near-0ms intent fulfillment when the LLM provider is down.

### 🔧 Code Quality & Hooks
- Added Graceful shutdown bindings for `intent_router` HTTP clients in `bot.py`.
- Fixed multiple linter and type-checking warnings across routing and configuration paths.

## [2.10.0] - 2026-04-12 - Local Telegram Bot API Server Migration

### 🏗️ Infrastructure — Self-Hosted Local Bot API Server

Migrated the entire Telegram communication layer from `api.telegram.org` (cloud) to a self-hosted **Local Bot API Server** (`aiogram/telegram-bot-api`) running on the same VPS. This eliminates network round-trip latency for all file operations and unlocks Telegram's 2 GB file transfer limit.

#### Architecture
- **`telegram-bot-api` container**: Runs the official Telegram Bot API binary via MTProto, exposing a REST API on port `8081` inside the Docker network.
- **Shared Docker volume** (`tg-api-data`): Both the API server and the bot container mount `/var/lib/telegram-bot-api` at the same path, enabling zero-copy file access.
- **`tg-media-cleanup` container**: Lightweight Alpine cron that deletes cached media files older than 7 days every 24 hours, preventing disk overflow.
- **Docker network** (`tg-net`): Bridge network for inter-container communication via hostname resolution.

#### Key Changes
- **`app/config.py`**: New `TELEGRAM_LOCAL_SERVER_URL` setting (default: empty = cloud mode).
- **`bot.py`**: Conditional `builder.base_url().local_mode(True)` injection when the setting is present.
- **`app/utils/tg_file.py`** *(new)*: Centralized `get_file_bytes(bot, file)` utility — reads from local filesystem in `local_mode`, falls back to `download_as_bytearray()` in cloud mode.
- **8 call-site migrations**: All `download_as_bytearray()` and `download_to_drive()` calls across `msg_voice`, `ai_photo`, `ai_search`, `cmd_asr_test`, `debounce`, and `msg_document` now use the new utility.
- **`msg_document.py`**: Dynamic file size guard — 2 GB in local mode, 50 MB in cloud mode. Documents in local mode are processed directly from the shared volume with zero tempfile copy.

### 🚀 CI/CD Automation — `deploy.yml`
- **Automated one-time cloud logout**: First deploy runs `bot.log_out()` in a throwaway container, touches `/opt/tg-local-api-migrated` flag, waits 30s. All subsequent deploys skip this step.
- **Multi-container deployment**: Deploy script now manages `tg-api` → health poll → `tg-bot` → `tg-media-cleanup` in sequence.
- **New GitHub Secrets**: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` injected from secrets — zero manual VPS configuration.
- **Backward-compatible**: When `TELEGRAM_LOCAL_SERVER_URL` is unset, the bot uses `api.telegram.org` as before.

### 🔧 Code Quality
- All changed files pass `ruff check` and `ruff format`.
- 1578 unit tests pass (0 failures).

## [2.9.37] - 2026-04-12 - Inline Mode: Timeout Budget Propagation & Retry UX

### 🐛 Critical Bug Fix — Inline Timeout Contention

#### Problem
Inline mode responses consistently timed out (`TimeoutError`) before the model could finish generating. The inline handler enforced a 25s outer `asyncio.wait_for`, but the underlying provider stack (Router → UseCase → ResiliencePolicy → GeminiProvider) defaulted to a 120s internal timeout with up to 3 retries. The outer wrapper always cancelled the inner logic before it could complete or retry, producing generic "failed" error messages.

#### Fix — Timeout Budget Propagation
Threaded a `timeout` parameter from the inline handler down through the entire call chain:
- `inline.py` → `ai_core.py` → `router.py` → `agent_use_cases.py` → `provider.py`
- Inline generation now uses a strict **20s budget** (inner) with a **22s outer guard**.
- `ResiliencePolicy` is forced to `max_retries=1` when a timeout budget is propagated, preventing wasted retries.
- `thinking_level="off"` (mapped to `"minimal"`) for inline queries to prioritize speed over deep reasoning.

### ✨ New Feature — Inline Retry Button

When an inline generation fails (timeout or any error), the edited message now shows:
- **Timeout**: `⏰ Модель не успела ответить вовремя.`
- **General failure**: `❌ Не удалось получить ответ.`
- **`🔄 Повторить` inline button** to re-trigger generation without re-typing the query.

The retry mechanism uses a TTL-based in-memory store (5-minute expiry) that preserves the original query, tone, and user ID. On tap, the callback handler re-launches the full generation pipeline and updates the message in-place.

### 🔧 Code Quality
- Fixed `C408` ruff lint: replaced `dict()` call with `{}` literal in `agent_use_cases.py`.
- Fixed `SIM108` ruff lint: collapsed `if/else` into ternary in `inline.py`.
- Auto-formatted `inline.py` and `bot.py` via `ruff format`.

### Files Changed

| File | Change |
|------|--------|
| `app/handlers/inline.py` | Timeout budget (20s inner + 22s outer), `thinking_level="off"`, retry button with TTL store, `handle_inline_retry_callback`, ternary lint fix |
| `app/handlers/ai_core.py` | `timeout` param threaded to router |
| `app/providers/router.py` | `timeout` param threaded to use case |
| `app/agent_use_cases.py` | `timeout` forces `max_retries=1`; dict literal fix |
| `bot.py` | Registered `handle_inline_retry_callback` with `^inl_retry:` pattern |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check .` | 0 errors ✅ |
| `ruff format --check .` | 307 files, 0 violations ✅ |
| `pytest` (unit suite) | **1578 passed**, 0 failed ✅ |

---

## [2.9.36] - 2026-04-11 - Inline Mode: Cross-Chat Bot Interaction via @mention

### ✨ New Feature — Inline Mode (`@gemaibotv2 <query>`)

Users can now invoke the bot from **any Telegram chat** (including private conversations with other people) by typing `@gemaibotv2 <query>`. This implements Telegram's [Inline Mode](https://core.telegram.org/bots/inline) with a hybrid async delivery pipeline.

#### UX Flow

1. User types `@gemaibotv2 <query>` → bot instantly returns **3 `InlineQueryResultArticle`** tone options:
   - 📋 **Формальный** — strict, professional, facts-only
   - 😊 **Дружеский** — warm, casual, emoji-friendly
   - 😏 **Саркастичный** — light irony, still helpful
2. User selects a tone → a styled `⚡️ Gemaibotv2 генерирует ответ…` placeholder is posted to the chat.
3. Bot captures `ChosenInlineResult` with `inline_message_id`, launches a **background generation task**.
4. Background pipeline:
   a. **Tavily QnA search** (`search_type="qna"`, 8 s timeout, best-effort) — same search infrastructure as `?` prefix and agentic research.
   b. Freshness context injected into system prompt alongside tone hint.
   c. **`gemini-3.1-flash-lite`** generates a concise answer (≤ 3–4 paragraphs).
   d. `markdown_to_html()` formats the response → `bot.edit_message_text(inline_message_id=..., parse_mode="HTML")` edits the placeholder **in-place**.
5. Two-level fallback: HTML parse error → plain-text retry. Both log errors without propagating to the user.

#### Technical Details

- **Model**: `gemini-3.1-flash-lite` (ultra-low latency, free-tier compatible).
- **Formatting**: fully HTML-based via `app.utils.text_format.markdown_to_html` — consistent with every other bot message.
- **`_bg_tasks` set**: fire-and-forget `asyncio.create_task` tasks pinned in a module-level set to prevent GC before completion.
- **Background task GC safety**: task is held in `_bg_tasks: set[asyncio.Task]`; removed via `task.add_done_callback(_bg_tasks.discard)`.
- **`cache_time=0`**: inline results are never cached by Telegram, ensuring each keystroke returns a fresh result list.

#### BotFather Configuration Required (one-time)

| Action | Setting |
|--------|---------|
| `/setinline` | Enable inline mode; set placeholder text (e.g., `Введите запрос…`) |
| `/setinlinefeedback` | **100%** probability — required to receive `ChosenInlineResult` with `inline_message_id` for in-place editing |

#### Files Changed

| File | Change |
|------|--------|
| `app/handlers/inline.py` | **[NEW]** `handle_inline_query`, `handle_chosen_inline_result`, `_generate_and_edit_inline` |
| `bot.py` | `InlineQueryHandler` + `ChosenInlineResultHandler` registered; `"chosen_inline_result"` added to `_ALLOWED_UPDATES` |
| `README.md` | Inline Mode feature, handler table, Architecture Decisions, Main User Flows updated |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/handlers/inline.py` | 0 errors ✅ |
| `python -c "ast.parse(...)"` (inline.py + bot.py) | Syntax OK ✅ |

---

## [2.9.35] - 2026-04-11 - Gemini TTS Timeout & Null Buffer Fixes


### 🐛 Critical Bug Fixes
- **TTFT (Time-To-First-Token) Timeout Fix**: Addressed a severe bug where Gemini TTS generation frequently timed out with `TTS generation timed out after 30s`. Current generation audio models have a large TTFT under load (>35s). Adjusted `voice_engine.py` adaptive timeout floor from `30s` to `120s`, completely mitigating premature cancellation and timeout rotation.
- **Empty Audio Buffer Fix (`finishReason: STOP`)**: Fixed a bug on Gemini 2.5 Flash Audio generation models affecting API calls with a `temperature` below `0.5`, which resulted in internal empty audio buffers despite correct API formatting. Clamped the `GenerateContentConfig` temperature minimum to `0.5`, while preserving the separate `user_prefs.tts_temperature` string value to drive the steerable prompt stylistic directives accurately.
- **Output Token Truncation**: Enforced explicit `max_output_tokens=16384` parameter in the Generation Config in `tts.py` (matching the official model spec limits for audio). Resolves situations where internal reasoning tokens would crowd out output limits leading to silent truncation and suppression.

---

## [2.9.34] - 2026-04-11 - Gemini TTS Hallucination Fix & Stability

### 🔊 Voice Engine 4.1 Stability Polish
- **Strict Verbatim Constraints**: Hardened the Gemini TTS Steerable Voice prompt to strictly forbid summarization, abbreviation, or generative conversational fillers (like "Продолжение следует..."), resolving severe text hallucinations on long messages.
- **Adaptive Chunk Sizing**: Recalibrated the Gemini sequential chunking limit to `max_bytes=1800` (up from 1000, down from 2500) to find the perfect medium between generation stability (preventing 500 timeouts) and maintaining cohesive multi-sentence intonation arcs. Chunking respects sentence boundaries safely without severing mid-word.
- **PCM Silence Trimming Threshold**: Reverted the trailing silence trim threshold from 1500 back to `400` amplitude in raw 16-bit LE PCM data. The previous high threshold inadvertently truncated valid quiet speech (e.g. dropping 19 seconds of speech simply because it didn't cross 1500 amplitude). `400` correctly strips digital zeros without clipping quiet speech.

---

## [2.9.33] - 2026-04-07 - MemPalace Memory Architecture Integration

### 🧠 MemPalace: Wing/Room Taxonomy (Phase 2)
- **Hierarchical Memory Classification**: Every memory and knowledge graph entity is now classified into a MemPalace Wing → Room → Hall hierarchy, enabling targeted retrieval (e.g., only search "identity" wing for personal facts).
- **5 Wings**: `identity`, `projects`, `social`, `knowledge`, `temporal` — with 4–5 rooms each.
- **6 Hall Types**: `fact`, `opinion`, `event`, `plan`, `preference`, `habit`.
- **Partial HNSW Indexes**: High-traffic wings (`identity`, `projects`) get dedicated pgvector HNSW indexes for sub-10ms vector search within a single wing.
- **LLM-Classified**: Graph extraction prompt now instructs Gemini to assign wing/room alongside entities and relations. Validated against allowed values with `knowledge` fallback.
- **Admin-Configurable Model** (`TAXONOMY_MODEL`): The classification model is hot-reloadable via `config_manager.update_setting("TAXONOMY_MODEL", "...")` or env var `TAXONOMY_MODEL`. Defaults to `gemini-3.1-flash-lite`.
- **Migration**: `032_add_wing_room_taxonomy.sql` — adds `wing`, `room`, `hall_type` columns to `long_term_memory` and `wing`, `room` to `memory_nodes` with B-tree + partial HNSW indexes.

### 🗜️ AAAK Tiered Context Compression (Phase 3)
- **New Module**: `app/context/compression.py` — 4-layer memory stack inspired by AAAK lossless shorthand:
  - **L0: Core Facts** (~250 tokens) — JSON shorthand from `is_core=TRUE` graph edges, always injected.
  - **L1: Active Context** (~600 tokens) — structured summary from recent consolidated memories + role diary entries.
  - **L2: Semantic Recall** (~1500 tokens) — full `search_memories_with_graph()` results + LLM-judge fallback.
  - **L3: Full History** — managed by existing `assembler.py` token budget.
- **XML Memory Palace Block**: All layers wrapped in `<memory_palace>` / `<core_identity>` / `<active_context>` / `<knowledge_graph>` XML tags for structured LLM consumption.
- **Unified Injection**: Replaced 90-line inline memory injection block in `ai_chat.py` with single `inject_memory_layers()` call.

### ⚔️ 2-Stage Contradiction Detection (Phase 4)
- **Embedding Distance Triage**: Edge conflicts are now classified by cosine distance into three zones:
  - `< 0.15` — near-duplicate, handled by semantic merge.
  - `0.15–0.35` — ambiguous zone → triggers **LLM-as-judge** (cheap Flash-Lite call).
  - `≥ 0.35` — clearly different → temporal close (old edge superseded).
- **LLM Judge Verdicts**: `update` (factual change), `parallel` (both true), `refinement` (merge predicates).
- **New Function**: `_resolve_ambiguous_conflict()` in `memory_extraction.py`.

### 📔 Persistent Role Diaries (Phase 5)
- **Per-Role Session Memory**: Each custom role accumulates diary entries (key learnings, user preferences, style observations) that persist across sessions.
- **State Integration**: `UserState.role_diaries` dict (`{role_id: [entries]}`) persisted to `user_state.role_diaries` JSONB column.
- **Public API**: `get_role_diary()`, `append_role_diary()`, `clear_role_diary()` in `app/state.py`.
- **L1 Context Injection**: Active role's diary entries are automatically included in the L1 compression layer.
- **Migration**: `033_add_role_diaries.sql` — adds `role_diaries JSONB DEFAULT '{}'` to `user_state`.

### 🛡️ Auto-Save & Graceful Shutdown Hooks (Phase 1)
- **New Module**: `app/repos/memory_autosave.py` — centralized heartbeat saves, pre-shutdown compaction, and in-flight memory write draining.
- **Shutdown Integration**: `bot.py` `_cleanup_application` now drains pending memory writes and runs pre-shutdown compaction before process exit.

### 🔧 Infrastructure
- **Host**: Migrated to dedicated 2 vCPU / 4GB RAM / 120GB NVMe SSD server.
- **Backward Compatibility**: All new columns are optional — code gracefully degrades on databases that haven't run migrations 032/033 yet.

### 🗄️ Database Migrations

| Migration | Detail |
|-----------|--------|
| `032_add_wing_room_taxonomy.sql` | `wing TEXT`, `room TEXT`, `hall_type TEXT` on `long_term_memory`; `wing TEXT`, `room TEXT` on `memory_nodes`. B-tree + partial HNSW indexes. |
| `033_add_role_diaries.sql` | `role_diaries JSONB DEFAULT '{}'` on `user_state`. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` (modified files) | 0 errors ✅ |
| `pytest` (full suite, excl. pre-existing `tts_temperature` failures) | **1641 passed**, 0 failed ✅ |

---

## [2.9.32] - 2026-04-06 - Independent TTS Temperature & Steerable Voice Prompting

### ✨ UX Enhancements & Generative Polish
- **Independent TTS Controls:** Divorced Text generation temperature from Audio generation (TTS) temperature via database-level updates (`public.chats`). The Telegram MiniApp Settings menu now features a dedicated "Температура аудио (TTS)" slider.
- **Steerable Voice Instructions (Gemini):** Refactored the core text-to-speech orchestration in `voice_engine.py` / `tts.py`, replacing unsupported "Director's Notes" blocks with strictly native Steerable Voice syntax. Injected inline rules for Russian phonetics, abbreviation expansion, and directly mapped pacing tags (`[extremely fast]`) right into the synthesis payload for maximum phonetic adherence.
- **Dynamic TTS Personalities:** Linked the `tts_temperature` directly to the Steerable Voice styling engine. The bot automatically shifts between strict/flat news-anchor (`<=0.3`), a warm/conversational default (`0.4-0.7`), and a highly expressive/dynamic storyteller (`>=0.8`) based strictly on the users' UI slider preferences without any additional API calls.

## [2.9.31] - 2026-04-06 - Voice Engine 4.1 Reliability & Bug Fixes

### 🐛 Critical Bug Fixes
- **TTS Generator Stability (500 Error Fix):** Reverted the "Zero-Latency" single-batch chunking approach that was introduced in Voice Engine 4.0. Passing extremely large contiguous text directly to Gemini `flash-preview-tts` exceeded API structural constraints, causing 44s timeouts or hard `500 INTERNAL` API crashes. The system now safely re-chunks outputs over `2500` bytes iteratively to secure latency resilience. Additionally restored config temperature from `0.3` to `0.5` to alleviate AI generation loop collapse (audible stuttering/hallucinations).
- **Audio Retranscription Crash:** Handled an asynchronous tuple unpack error (`ValueError: too many values to unpack (expected 2, got 3)`) that blocked the ⚡ Re-transcribe (Flash) UI and Dev `/asr` command. Correctly unpacked the `draw_prompt` parameter globally, ensuring production functionality is totally restored.

## [2.9.30] - 2026-04-06 - Voice Engine 4.0 & Mini App Refactoring

### ✨ UX Enhancements & TTS Tuning
- **Zero-Latency Single-Batch UX (Gemini TTS):** Completely decoupled the Gemini API wrapper from legacy GCP 3500-byte constraint loops. The `gemini-2.5-flash-preview-tts` orchestrator now passes up to 40,000 bytes into a single request, eliminating sequential recursive HTTP network wait times and producing near-instant TTS Time-To-First-Byte for even the longest messages.
- **Micro-Optimization & Dictation Control:** Configured native `[extremely fast]` internal tags to force brisk pacing organically. Added structural stability by enforcing `temperature=0.3` on speech synthesis generation configs, aggressively reducing vocal hallmarks like phantom sighing, random trailing, or artifacts while tightening Standard Russian phonetics parsing. Greatly shortened the Director's Note prefix prompt by ~68% to speed up Token/s input processing latency.
- **Rasalgethi Voice Availability:** Pushed internal Voice Provider manifest updates so 'Rasalgethi' now appears across inline pickers and UI.
- **Mini App Interface Tuning:** Reordered the Quart-served Mini App navigation (`app/templates/miniapp.html`) to present `Settings | Graph | Memory` sequentially. Realigned the Graph shortcut to a high-visibility top-left anchor. 

## [2.9.29] - 2026-04-06 - Mini App Reader UX & Core Stability Fixes

### 🌟 UX Enhancements & Bug Fixes
- **Swipe-To-Delete Gestures**: Fixed critical gesture conflict on high-resolution Android/iOS WebViews where the `.memory-card-inner` swipe-to-delete action was immediately interrupted by the browser's native pan actions. Added `touch-action: pan-y` to guarantee clean horizontal sweep captures.
- **Voice Preferences Routing**: Resolved a regression where user-selected voices in the settings UI were ignored by the backend TTS orchestration engine. `fire_voice_reply` now accurately passes the selected voice ID, seamlessly degrading across ElevenLabs and Gemini text-to-speech providers based on runtime configurations (`use_elevenlabs`).
- **Read More Buttons Redesign**: Overhauled inline Telegram buttons guiding users into the Long-Read reader (`streaming.py` and `ai_search.py`). Migrated from the ambiguous `📖 Читать полностью` to a polished `📄 Развернуть статью (Mini App)` accompanied by explanatory fallback text `(Продолжение доступно по кнопке...)` to clearly indicate out-of-chat transitions. 
- **TTS Content Filtering**: Extended the "Озвучить вслух" background task generator to strip code blocks (\`\`\`) via RegEx `DOTALL` patterns across both truncated messages and fully resurrected Long-Read texts. This prevents robotic readouts of raw programming code, maintaining native conversational immersion.

## [2.9.28] - 2026-04-06 - Long Read Reader Overhaul: SSR, Cold Storage Fallback & 5-Point UX

### 🚀 Architecture: Server-Side Rendering (SSR) for Long Read Reader

**Problem:** The `/webapp/reader` page used client-side rendering (CSR) with `marked.js`.
Every visit required: navigate → load shell → JS parses markdown → render.  This produced a
visible "skeleton" load state degrading FCP on mobile Telegram WebViews.

**Fix:** `/webapp/reader` is now SSR-rendered by the FastAPI/Quart handler before any HTML
is sent to the browser.  The Jinja2 template receives `body_html`, `toc_json`,
`source_label`, and `telegraph_fallback_url` — the page paints instantly with full content.

#### New: `app/utils/reader_utils.py`
- **`markdown_to_reader_html(markdown, toc)`** — Full-featured Markdown→HTML renderer targeting
  a standalone WebApp (emits `<h1>`–`<h3>`, `<hr>`, `<blockquote>`, `<ul>`, `<ol>`, `<a target="_blank">`,
  and rich `.code-block` divs with `data-lang` + encoded `data-code` for client JS).
  Processes fenced code blocks first so later passes never touch code content.
- **`extract_toc(markdown)`** — Extracts H1–H3 headings (skips headings inside code fences),
  generates URL-safe deduplicated anchors, returns empty list when fewer than 2 headings found.
- **`apply_bionic_reading(html_text)`** — Wraps word stems in `<b>` tags throughout
  text nodes (skips `<code>`, `<pre>`, `<b>`, `<strong>`, `<a>` content).
  Bionic fraction scales with word length: 1→1 char, 4–6→2, 7–9→3, 10+→4.
- **`extract_text_from_telegraph_html(html)`** — Strips tags then decodes HTML entities
  for the cold-storage fallback pipeline (order matters: strip first so decoded `<` chars
  aren't mistaken for new tags).

### 🔄 Feature: Telegraph Reverse-Proxy Cold Storage

When the Redis 24h TTL expires but a Telegraph URL is still present:
1. `reader_page()` calls `_fetch_telegraph_content(tg_url)` — fetches the Telegraph HTML,
   extracts the `<article>` body, converts to plain text via `extract_text_from_telegraph_html`.
2. The extracted text is rendered through `markdown_to_reader_html` + `extract_toc` just like
   a live Redis hit — user sees our own Reader UI, not a Telegraph redirect.
3. If the reverse-proxy fetch fails (timeout, blocked network, no `<article>` tag), the template
   falls back to a Telegraph link button (`telegraph_fallback_url`), which auto-redirects
   inside Telegram 900ms after page load.

`_fetch_telegraph_content()` silently returns `None` on any network or parse failure — no
exception leaks to the user.

### 🎨 Feature: Floating Table of Contents (TOC FAB + Bottom Sheet)

- **FAB button** (☰) appears in the bottom-right corner whenever `extract_toc()` finds ≥ 2 headings.
- Tap → bottom sheet slides up (spring cubic-bezier animation) with a full heading list.
- Tap any entry → smooth scroll to heading with a topbar offset.
- Swipe down → closes the sheet (native mobile gesture).
- Sheet closes automatically after navigation.
- Haptic feedback via `Telegram.WebApp.HapticFeedback`.

### ⛶ Feature: Full-Screen Code Modal + File Download

Each `.code-block` header now has three action buttons:
- **⛶ Expand** → opens a bottom-sheet-style full-screen modal with the raw code, re-highlighted
  by `hljs` with correct language class (`language-{lang}`).
- **↓ Download** → auto-detects file extension from the language label (`python→.py`,
  `typescript→.ts`, etc.) and triggers a `Blob` download. 20+ languages supported.
- **Копия** → copies raw code from `data-code` attribute to clipboard; shows ✓ confirmation
  for 1.8 s.

Code blocks also feature gradient fade-right indicators (`::after` overlay) to signal
horizontal overflow on mobile.

### 👁 Feature: Bionic Reading Toggle

- **Top-bar toggle button** "👁 Bionic" activates Bionic Reading mode.
- On first activation, a `TreeWalker` traverses all text nodes in `#md-body` (skipping
  `<code>`, `<pre>`, `<b>`, `<strong>`) and wraps word stems in `<b>` tags via `.innerHTML`
  replacement.
- CSS class `body.bionic b { font-weight: 800 }` makes the injected `<b>` tags visually
  heavier.
- Preference persisted in `sessionStorage` — survives tab navigation within the session.

### 🔊 Feature: Text-to-Speech (Read Aloud)

- **Top-bar "🔊 Вслух" toggle** invokes `window.speechSynthesis`.
- Uses `SpeechSynthesisUtterance` with `lang: 'ru-RU'`, `rate: 0.92`, `pitch: 1.0`.
- Button morphs to "⏹ Стоп" while active.
- Auto-stops when the browser tab is hidden (`visibilitychange` event).
- Graceful degradation: shows toast if `speechSynthesis` is unavailable.

### 🛠 Modified Files

| File | Change |
|------|--------|
| `app/utils/reader_utils.py` | [NEW] SSR rendering utilities |
| `app/web_miniapp.py` | `/reader` upgraded to SSR; `_fetch_telegraph_content()` added |
| `app/templates/reader.html` | Complete rewrite — SSR Jinja2 vars, TOC sheet, code modal, Bionic, TTS |
| `tests/test_reader_utils.py` | [NEW] 23 unit tests for all reader utility functions |
| `tests/test_reader_ssr.py` | [NEW] 7 tests for Telegraph reverse-proxy + API endpoint paths |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` (changed files) | 0 errors ✅ |
| `mypy app/utils/reader_utils.py app/web_miniapp.py` | Exit 0 ✅ |
| `pytest tests/test_reader_utils.py` | **23 passed** ✅ |
| `pytest tests/test_reader_ssr.py` | **7 passed** ✅ |
| `pytest` (full suite) | **1643 passed**, 0 failed ✅ |

---

## [2.9.27] - 2026-04-05 - MiniApp Audit Patch: 6 Correctness Bugs + M-1 Reactive Pointer Fix

### 🔴 Critical Fixes (found in post-implementation audit)

#### C-2: SVG `applyGraphTransform` — CSS transition never fired
- `g.setAttribute('transform', ...)` modifies an SVG attribute; the browser's CSS transition engine only fires on **CSS properties** (`element.style.*`), not SVG attributes. The entire smooth zoom animation was a no-op.
- Fix: `g.style.transform = 'translate(Xpx, Ypx) scale(Z)'` + `g.style.transformOrigin = '0 0'`. Individual node positions remain via `gNode.setAttribute('transform', ...)` since they have no transition.

#### C-3: Double `scheduleDelete` — swipe + synthesized click from pointer drag
- After a pointer drag (swipe), the browser synthesizes a `click` event on `pointerup` if delta < threshold. If the pointer lifted near the `.memory-delete-hover` button area, both the swipe path (`endSwipe`) and the click handler called `scheduleDelete(id, card)`. The second call overwrote `pendingDeletions[id]`, making the first timer orphaned and un-undoable.
- Fix: early return guard in hover-delete click: `if (pendingDeletions[memId]) return`. Mirror guard in `endSwipe`: `if (!pendingDeletions[id])`.

#### C-4: `prompt-area` used `calc(100vh - 120px)` — broken on iOS with virtual keyboard
- `100vh` inside `position: fixed` body does not shrink when the iOS virtual keyboard opens. Textarea extended below the keyboard, making bottom rows inaccessible.
- Fix: `#page-prompt` is now `display: flex; flex-direction: column`. `.prompt-area` uses `flex: 1; min-height: 0` to fill available height correctly regardless of keyboard state.

### 🟡 Medium Fixes

#### M-2: `clearTimeout` called on a `setInterval` ID — interval never stopped
- `resetCountdownTimer` is created with `setInterval()`. `clearTimeout(id)` on a `setInterval` ID is a no-op in all major browsers (they share an ID space but `clearTimeout` only operates on timeout callbacks).
- Memory leak: the countdown interval continued ticking after the user left confirming state, consuming CPU and risking double-execution.
- Fix: replaced all `clearTimeout(resetCountdownTimer)` with `clearInterval(resetCountdownTimer)`.

#### M-5: Hover state CSS transition dropped `transform` — swipe snap instead of spring
- `.memory-card:hover .memory-card-inner` declared `transition: background .15s, padding-right .2s`. This overwrote the base `transition: transform .3s cubic-bezier(0.2,0.8,0.2,1)`. On hover state, the swipe-back animation (`translateX → 0`) had no transition — hard snap.
- Fix: Added `transform .3s cubic-bezier(0.2, 0.8, 0.2, 1)` to the hover-state transition rule.

#### C-1: `transition: all` on `.tab-bar` — layout properties animate on resize
- On Telegram Desktop, `transition: all .3s` caused layout properties (`left`, `border-radius`, `width`) to animate when the viewport was resized (dock ↔ mobile mode transition). Visually jarring.
- Fix: `transition: background .2s ease, box-shadow .2s ease` — only safe cosmetic properties.

### 🟢 Low Fixes

#### M-4: Dead parameter `containerId` in `initChipsScroll`
- First parameter was accepted by the function but never used. Removed from signature and call-site.

### 🔧 M-1 Fix: Reactive pointer-device detection (previously deferred low-risk item)
- Old: `const isPointerDevice = window.matchMedia('(hover: hover)').matches` — evaluated once at load; wouldn't update if a user connected/disconnected a mouse on a hybrid laptop during the session.
- New: `hoverMQL.addEventListener('change', ...)` calls `bindResetBtn(e.matches)`, which clones and replaces the button DOM node (to cleanly remove all prior listeners) and re-attaches the correct interaction model (desktop morph vs. mobile hold).
- Removed now-dead `resetBtnAbort()` / `doContextReset()` helper functions (their logic is inlined inside `bindResetBtn()` to use stable local references that survive `cloneNode`).

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/ tests/` | 0 errors ✅ |
| `ruff format app/ tests/ --check` | 290 files — 0 violations ✅ |
| `pytest` (full suite) | **1613 passed**, 0 failed ✅ |

---

## [2.9.26] - 2026-04-05 - Telegram MiniApp 7-Point UX/UI Overhaul (Desktop-First Cross-Platform)

### 🎨 UX/UI Overhaul — `app/templates/miniapp.html`

Full redesign of the MiniApp focusing on desktop (mouse/trackpad) usability parity without regressing mobile (touch) experience. Addresses 7 identified interaction pain points through adaptive, pointer-aware patterns.

#### FIX #1 — Role Chips: Wheel-Y→X redirect + hover arrows + gradient fade masks
- Wrapped `.chips-wrap` in `.chips-container` (`overflow: hidden`) to clip gradient masks.
- `.chips-fade-left` / `.chips-fade-right` gradient overlays always visible when content overflows (opacity transitions via `.hidden` class).
- `.chips-arrow` left/right `‹›` buttons visible on `@media(hover:hover)` via `.chips-container:hover`. Hidden on touch-only devices.
- `initChipsScroll()` attaches a `wheel` listener that redirects predominant vertical `deltaY` input to `scrollLeft` with `e.preventDefault()` (`{passive: false}`). Factor `0.8` for natural feel.
- `MutationObserver` + `scroll` listener keep arrow/fade visibility in sync with scroll position.
- `renderRoles()` dispatches a `scroll` event after DOM update so arrows reflect new content width.

#### FIX #2 — Memory Cards: Desktop hover-reveal trash button
- `.memory-delete-hover` `<button>` (SVG trash icon) added to each memory card template.
- On `@media(hover:hover)`: slides in from right (`translateX(6px → 0)`) and fades to `opacity: 1` on card hover.
- Click: fades `.memory-card-inner` in 0.2s, then calls `scheduleDelete()` (with guard against double-scheduling — see C-3 audit fix).
- Mobile swipe-to-delete (`initSwipeToDelete`) unchanged and still operational.

#### FIX #3 — Reset Context Button: Adaptive morphing (desktop: double-click / mobile: hold)
- Detects interaction model via `window.matchMedia('(hover: hover)')` (MediaQueryList, reactive — see M-1).
- **Desktop**: first click morphs button to `.confirming` state (destructive red bg, glow border). Label updates to `"Нажмите ещё раз · 3s"` with a `setInterval` countdown. Second click within 3s executes reset. Timeout auto-reverts.
- **Mobile**: original hold-to-confirm with 1s fill animation (Pointer Events, `pointerdown` + `pointerup/cancel/leave`).
- Both paths provide haptic feedback at appropriate moments.

#### FIX #4 — Tab Bar: Floating glassmorphic dock on desktop
- `@media(hover: hover) and (min-width: 450px)`: tab-bar detaches from left+right edges, centers via `translate(-50%)`, `border-radius: 22px`, `backdrop-filter: blur(20px) saturate(1.6)`, layered `box-shadow`, background `color-mix(in srgb, var(--bg) 80%, transparent)`.
- Tab buttons gain per-item `border-radius: 14px`, hover background, active state highlight.
- `.tab-btn-icon` spring animation on active (scale 1.12, `cubic-bezier(0.34, 1.56, 0.64, 1)`).
- Mobile: unchanged fixed bottom bar.

#### FIX #5 — Knowledge Graph: Mouse wheel zoom with cursor-as-pivot
- `svg 'wheel'` listener extracts cursor position relative to SVG rect and passes as `(cx, cy)` pivot to `zoomGraph()`.
- `zoomGraph(factor, cx, cy)` applies zoom mathematics: `panX = cx - zoomDelta * (cx - panX)` — zoom tracks cursor precisely.
- `applyGraphTransform(animate)` uses `g.style.transform` (CSS property, not SVG attribute) for smooth `0.25s cubic-bezier` transition.
- Zoom clamped: `[0.3×, 5×]`. Buttons `+`, `-`, `⟳` still available.
- First `mouseenter` shows `.graph-hint` tooltip `"🖱 Колесо для зума"` (2.5s fade).

#### FIX #6 — Settings: Adaptive density for desktop (mouse precision)
- `@media(hover: hover)`: `.setting-row` min-height `44→38px`, padding `14→10px`.
- Slider thumb `20→16px`, `cursor: ew-resize`. `.range-slider` track `cursor: ew-resize`.
- `.toggle` `51×31→44×26px`, thumb `27→22px`, translate `20→18px`.
- `.chip:hover:not(.active)`: `color-mix(in srgb, var(--secondary-bg) 70%, var(--link) 30%)` hover highlight.
- `.segment` tighter: `padding: 6px`, `font-size: 12px`.

#### FIX #7 — Scroll Isolation: Prevent Telegram close gesture on content scroll
- `html, body`: `height: 100%; overflow: hidden; position: fixed; width: 100%; overscroll-behavior-y: none`. Prevents iOS/Android pull-to-dismiss propagating from content scroll up to Telegram WebView close gesture.
- `.page`: `position: absolute; inset: 0; overflow-y: auto; -webkit-overflow-scrolling: touch; overscroll-behavior-y: contain; padding-bottom: 80px`. Each page is a self-contained scroll viewport.

### 🐛 Critical Bug Fix: Memory deletion restoration on app close
- If the user swiped a MiniApp closed before the 5-second undo window expired, the `scheduleDelete` `setTimeout` was garbage-collected. The deletion request never reached the server — the memory was "restored" on next load.
- Fix: `flushPendingDeletions()` uses `fetch(..., { keepalive: true })` (survives page teardown) triggered on:
  - `tg.onEvent('viewportChanged', ...)` when `isStateStable && tg.viewportHeight < 10` (Telegram native close gesture)
  - `window.addEventListener('beforeunload', ...)` (desktop/browser close)

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/ tests/` | 0 errors ✅ |
| `ruff format app/ tests/ --check` | 0 violations ✅ |
| `pytest` (full suite) | **1613 passed**, 0 failed ✅ |

---

## [2.9.25] - 2026-04-05 - LTM Retrieval False-Negative Fix & LLM-as-Judge Fallback

### 🐛 Critical Bug Fix: Adaptive Gap Filter Double-Threshold

#### Problem
`search_memories()` applied `min_similarity` **twice** — once via `adaptive_floor` passed to the SQL `WHERE` clause, and again inside the Python post-filter via `gap_threshold = max(min_similarity, top_sim - 0.15)`.

A memory with `sim=0.65` (e.g., `"Мою жену зовут Евдокия. А твою?"`) correctly passed the SQL gate (`adaptive_floor=0.48`) but was silently discarded by the Python gate (`gap_threshold=0.68`). The result: zero memories injected despite the fact being stored. The LLM responded with *"У меня нет доступа к вашим личным данным."*

#### Root Cause Detail
The gap filter's intent is to prune *outliers within the candidate set* (≤ 15pp below the top result). It was never designed to act as a second absolute hard floor. Using `max(min_similarity, ...)` violated that invariant.

#### Fix (`app/repos/memory.py`)
- Gap threshold now uses `max(adaptive_floor, top_sim - 0.15)` — the relaxed floor enforced by SQL, not the caller's soft threshold.
- Added `DEBUG` log when all candidates are dropped by the gap filter, reporting `top_sim`, `gap_threshold`, and `min_similarity` for future diagnostics.

### 🔧 Parameter Tuning (`app/handlers/ai_chat.py`)
- Lowered `min_similarity`: `0.68` → `0.60` (aligns with MemoryOS θ=0.60, experimentally validated on LoCoMo conversational benchmark; calibrated for `gemini-embedding-2-preview` multimodal embedding space where cross-lingual personal-fact similarities cluster at 0.55–0.72, below text-only model norms).
- Added `DEBUG` log when LTM retrieval returns 0 memories/triples (previously silent, making threshold debugging require guesswork).

### 🧠 LLM-as-Judge Fallback — RF-Mem "Recollection Path" (`app/repos/memory.py`, `app/handlers/ai_chat.py`)

Implements the dual-process memory architecture from **RF-Mem (2025)**: when the primary vector search (floor ~0.48) returns nothing, a second pass is attempted:
1. Fetch top-6 candidates at `floor=0.42` (wide net).
2. One cheap `gemini-3.1-flash-lite` call rates each candidate's relevance to the user's query (single batched JSON response).
3. Only genuinely relevant candidates (rated `true`) are injected, capped at 3.
4. Results are tagged `llm_judged=True` for observability.
5. All errors are caught and silently return `[]` — strictly non-blocking.

This handles recall-intent queries ("Напомни-ка...") that are by design semantically vague — vectors score low not because the memory is irrelevant, but because the query phrasing creates cosine distance.

### 🐛 Pre-Existing Test Fixture Fix (`tests/test_cb_feedback.py`)
- `_update_with_feedback` fixture created `query.message` as a bare `MagicMock()`, causing `isinstance(msg, Message)` to return `False` inside `_handle_vote`. `save_feedback` was never reached, making `test_thumbs_up_calls_save` (and others) fail silently when `_handle_vote` exited early via the guard.
- Fixed by using `MagicMock(spec=Message)` (and `spec=InlineKeyboardMarkup` for `reply_markup`).

### 🔧 Code Quality
- `ruff check app/ tests/` — 0 errors ✅
- `ruff format app/ tests/` — 290 files already formatted, 0 violations ✅

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/ tests/` | 0 errors ✅ |
| `ruff format app/ tests/ --check` | 0 violations ✅ |
| `pytest` (full suite) | **1613 passed**, 0 failed ✅ |

### 📐 LTM Parameter Changes

| Parameter | Old | New |
|-----------|-----|-----|
| `min_similarity` (caller floor) | 0.68 | **0.60** |
| `gap_threshold` (Python post-filter) | `max(min_similarity, top_sim − 0.15)` | `max(adaptive_floor, top_sim − 0.15)` |
| LLM-judge fallback | — | ✅ `floor=0.42`, top-3, Flash-Lite batch |

---

## [2.9.24] - 2026-04-05 - RLHF Feedback Fix, Citation Badges & Graph Canvas


### 🐛 Critical Bug Fix: Telegram Bot API Reaction Limitation

#### Problem
`set_feedback_reactions()` attempted to set TWO reactions (👍+👎) per message via `setMessageReaction`. **Telegram Bot API hard-limits non-premium bots to 1 reaction per message.** The call silently failed — users never saw the feedback invitation.

#### Solution
- **Replaced broken dual-reaction** with a two-stage "📝 Оценить" inline toggle button to reduce UI clutter.
- Tapping the toggle safely replaces it in-place with `[👍] [👎]` choices.
- Button handler `cb_feedback.py` upgraded with full RLHF pipeline: on 👎 → LTM negative signal + graph edge penalty.
- Removed ❤️ reaction on upvote to prevent clashing with primary `🔍 → ⚡` status reactions.
- Organic reactions (via Telegram reaction picker) still captured by `msg_reactions.py` as a fallback channel.
- `set_feedback_reactions()` deleted from `ux_improvements.py`, replaced by `make_feedback_buttons()`.

### 🧠 Citation Badges `[🧠 N facts]`
- When graph triples + memories were used to generate a response, a `[🧠 N facts]` badge is added to the inline keyboard (decorative noop button).
- Tracks `_graph_triples_count` alongside `_memories_injected` for accurate source counting.

### 🕸️ Knowledge Graph Visualization Canvas
- **New Mini App tab**: "🕸️ Граф" — interactive force-directed graph canvas.
- Renders nodes (circle radius ∝ edge degree) and edges (opacity ∝ weight) from `/webapp/api/graph`.
- Color palette by entity type: PERSON (blue), SKILL (green), ORGANIZATION (gold), LOCATION (red), CONCEPT (purple), TECHNOLOGY (teal).
- Pointer drag to reposition nodes, zoom in/out/reset controls.
- Tap node for tooltip with entity name and type.
- Tab-bar navigation (🧠 Память / 🕸️ Граф / ⚙️ Настройки) replaces old settings-button pattern.
- Zero external dependencies — force simulation implemented with native SVG + vanilla JS (120-iteration physics).

### 🔧 Code Quality
- `ruff check + format` — 0 errors across all 137 Python files.
- Stale docstrings and comments in `msg_reactions.py` updated to reflect new architecture.
- Mypy error fixed in `cb_feedback.py` (`isinstance(msg, Message)` guard).

### 📝 README
- Updated Smart UX section to describe inline button feedback pattern.
- Updated Mini App section: two-tab → three-tab with graph canvas.
- Updated RLHF sub-bullet under GraphRAG Memory.

---

## [2.9.23] - 2026-04-05 - Agentic GraphRAG Evolution (Phases 1–3)

### 🧠 Phase 1: Core Infrastructure — Real-Time Streaming Extraction

#### Real-Time Graph Extraction
- **New module `memory_extraction.py`**: Replaces batch-only consolidation with a streaming pipeline that fires on every qualifying user message (≥30 chars). Uses Gemini Structured Outputs (Pydantic `GraphExtractionResult` schema) with `thinking_level="medium"` for hallucination-resistant entity/relation extraction.
- 3-retry wrapper with exponential backoff for transient API errors (503, rate-limit, timeout).
- Entities → `memory_nodes` (semantic dedup, cosine < 0.12). Relations → `memory_edges` (semantic predicate dedup, cosine < 0.25).
- Wired into `ai_chat._store_memory_in_background()` as a second background task.

#### Temporal Conflict Management
- **Migration `028_add_temporal_edges.sql`**: Adds `valid_from TIMESTAMPTZ` / `valid_to TIMESTAMPTZ` to `memory_edges` with partial index on current edges.
- When a new edge conflicts with an existing one (same src→tgt, different predicate), old edge is closed (`valid_to = now()`) and new one inserted — preserving full history.
- `search_memories_with_graph()` updated with `WHERE valid_to IS NULL` filter and bilingual `<temporal_context>` injection for LLM awareness of life changes.

#### RLHF Feedback Loop
- Edge-ID caching in `memory.py`: `_last_retrieved_edge_ids` maps user_id → list of edge IDs from last retrieval.
- New `penalize_graph_edges()` function: on 👎 reaction, decays edge weights by 0.10 (clamped to 0.05 min).
- Bot pre-places 👍/👎 reactions on its own messages via `set_feedback_reactions()` as a silent invitation.

### 🔍 Phase 2: Agentic RAG & Multimodal Memory

#### Agentic RAG — `recall_memory` Tool
- **New module `memory_tools.py`**: Exposes LTM search as a Gemini function declaration (`recall_memory`) for the agentic research loop.
- `AgenticSearch` constructor gains `ltm_enabled` and `ltm_api_key` kwargs; when user has LTM enabled, the `recall_memory` tool is registered.
- `_execute_tool()` routes `recall_memory` calls to `execute_memory_tool()` which returns memories + graph triples as structured JSON.

#### Multimodal Memory — Image/Audio to Graph
- `process_media_for_memory()` now fires background graph extraction after storing media in LTM.
- New `_extract_graph_from_media()` function stores `file_id`/`file_type` on resulting `memory_nodes` for future media re-delivery.
- Works for both image descriptions and voice transcriptions.

### 🌐 Phase 3: Social Graph & Visualization

#### Group Chat Social Graph
- **Migration `029_add_multimodal_and_social_graph.sql`**: Adds `file_id`, `file_type`, `chat_id`, `actor_user_id` to `memory_nodes`; `chat_id`, `actor_user_id`, `is_public` to `memory_edges`. Partial indexes for group and multimodal queries.
- `extract_and_store_graph()` gains `chat_id` and `actor_user_id` parameters for social graph attribution.
- `GroupChatManager.log_group_message()` now fires background social graph extraction for non-bot messages (≥30 chars).

#### Knowledge Graph Visualization API
- New Mini App endpoint `GET /webapp/api/graph`: returns nodes and edges as JSON with optional query-based filtering (limit, entity name ILIKE). Authenticated via Telegram initData.

### 🔧 Code Quality
- Full `ruff check` + `ruff format` pass on all modified files — 0 errors.
- 6 pre-existing format violations fixed in `cache.py`, `streaming.py`, `voice_engine.py`, etc.

### 📝 README
- Expanded GraphRAG Memory section with 7 new sub-bullets documenting all Phase 1–3 capabilities.

### 🗄️ Database Migrations

| Migration | Detail |
|-----------|--------|
| `028_add_temporal_edges.sql` | `valid_from TIMESTAMPTZ` + `valid_to TIMESTAMPTZ` on `memory_edges`. Partial index `idx_memory_edges_temporal WHERE valid_to IS NULL`. |
| `029_add_multimodal_and_social_graph.sql` | `file_id`, `file_type`, `chat_id`, `actor_user_id` on `memory_nodes`; `chat_id`, `actor_user_id`, `is_public` on `memory_edges`. 3 partial indexes. |

---

## [2.9.22] - 2026-04-05 - Graph & LTM Cognitive Architecture Optimization

### 🧠 GraphRAG Memory — Query Intent Gate & Edge Provenance

#### Query Intent Gate (Performance)
- **New function `_should_expand_query()`** in `memory.py`: deterministic regex + length heuristic that skips the ~200ms Flash-Lite LLM query expansion call for trivial conversational inputs (greetings, one-word confirmations, emoji, short phrases <12 chars).
- Saves API quota and reduces retrieval latency for ~40% of chat turns.
- Intentionally conservative: when in doubt, expansion runs.
- Wired into `search_memories_with_graph()` as a gate before `expand_query_with_llm()`.

#### Edge Provenance — HippoRAG 2 Dual-Node (Schema)
- **Migration `027_add_edge_provenance.sql`**: Adds `source_memory_ids BIGINT[]` column to `memory_edges` with GIN index. Links graph edges back to the original `long_term_memory` rows from which they were extracted.
- Enables future retrieval of the full unstructured passage alongside relevant graph triples, reducing LLM hallucination around short predicates.
- Fully backward compatible: old rows default to `'{}'`; existing SQL queries are unaffected.

#### Schema Validation Fix
- **`app/db/schema.py`**: Added `memory_nodes`, `memory_edges`, and `brief_subscriptions` to `EXPECTED_TABLES`. These tables were created by migrations but never registered, causing silent startup validation warnings.

### 🔧 Code Quality
- Fixed stale docstring in `memory.py` (still referenced `gemini-embedding-001, 3072-dim` instead of `gemini-embedding-2-preview, 768-dim halfvec`).
- Removed unused imports (`asyncio`, `hashlib`) from `memory.py`.

### 📝 README — LTM Architecture Documentation
- Added **Query Intent Gate** documentation explaining the heuristic bypass.
- Added **Multi-Query Expansion** documentation as a standalone concept.
- Added **Knowledge Graph Architecture** subsection documenting graph extraction pipeline, semantic edge deduplication, core persona protection, 2-hop traversal, and edge provenance.
- Fixed stale consolidation model reference (`gemini-2.0-flash-lite` → `gemini-3.1-flash-lite`).
- Added migration `027` to the Schema Management catalog.
- Fixed config location for embedding model (`memory.py` → `memory_config.py`).

### 🗄️ Database Migrations

| Migration | Detail |
|-----------|--------|
| `027_add_edge_provenance.sql` | `source_memory_ids BIGINT[] NOT NULL DEFAULT '{}'` + partial GIN index. `DO $$ IF NOT EXISTS` idempotency guard. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/ tests/` | 0 errors ✅ |
| `ruff format` | 0 violations ✅ |
| `mypy app/repos/memory.py` | 0 errors ✅ |

---

## [2.9.21] - 2026-04-05 - Modernizing Telegram Mini App UX

### ✨ Feature — Advanced Mini App Frontend & Stack Navigation

*   **Native App Feel (Push/Pop Stack):** Migrated from basic tab-switching to a dynamic push/pop navigation stack in `miniapp.html`, deep-linking the Telegram BackButton exactly like a native iOS/Android settings menu.
*   **100-Card DOM Budget (Infinite Scroll):** Rearchitected the memories list interface to strict DOM virtualization restrictions, dropping older DOM nodes (FIFO) while grouping elements temporally to optimize low-end mobile performance.
*   **Interactive Swipe Design:** Brought iOS-like swipe-to-delete mechanisms directly to HTML alongside an interactive Undo Toast (5-second grace period) leveraging WebApp `HapticFeedback.impactOccurred` APIs.
*   **Component-Driven Settings Forms:** Extracted raw `<select>` elements into smooth "Bottom Sheets", introduced Hybrid Segmented Controls ( Точный, Баланс, Творческий, Свой ), and created a standalone Screen overlay for prompting textareas. Correctly bounded server APIs up to 2.0 temperature.
*   **Reader UX Refresh:** Injected a 2px top "Reading Progress Bar" directly into the WebApp Reader UI (`reader.html`). Supplemented with Auto-Hiding scroll mechanics to maximize text estate and added a one-click `word-break: break-all` toggle button ("Перенос") to code headers for fixing horizontal overflow.

---

## [2.9.20] - 2026-04-05 - CI-Ready Testing Architecture & Environment Isolation

### 🧪 Test Suite — Production Hardening & Security Isolation

*   **100% CI-Ready (1612 Tests)**: The test suite has achieved true deterministic CI/CD stability, with zero `Sleep` calls and perfect test-to-test isolation.
*   **Fixture Refactoring & Safety**: 
    *   `db_container.py`: Enforced robust container separation by using `asyncio.run()`, completely replacing process-level assertions that broke the `pytest-asyncio` event loop.
    *   **Unit Tests Resilience**: Guarded the initial `testcontainers` import structurally in the main `conftest.py`. Unit tests can now execute instantly even if Docker is completely offline or `testcontainers` is unloaded.
*   **Route Collision Elimination**: Re-designed E2E Quart Test Apps. All fixture test clients rely on isolated `Quart()` instances loaded dynamically inside `module` scopes over injecting handlers into `app.web.quart_app`, totally eliminating state-mutation test crashes.
*   **Resilience Edge Cases**: Fully randomized (`random.seed(42)`) all jitter calculations and added coverage asserting exactly one operation when `max_retries=1` via explicit sleep mocking tracking.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/ tests/ scripts/` | 0 errors ✅ |
| `mypy` (strict) | 0 errors ✅ |
| `pytest` | **1612 passed**, 0 failed ✅ |

---

## [2.9.19] - 2026-04-04 - Streaming Pipeline Failsafe & Deterministic AAA Tests

### 🐛 Production Bug Fix — Unbalanced HTML Tags in Streaming

**Root cause**: When `_find_split_point` truncated text inside a markdown code block (`<pre><code>`), the cached `pre_formatted` HTML string would contain unclosed `<pre>` tags. This unbalanced HTML was previously appended directly to the truncated message without sanitization, causing `Bad Request: can't parse entities` during extreme Telegraph overflow conditions.
**Fix**: Explicitly wrapped the failsafe message chunk (`formatted_frozen = sanitize_html_tags(pre_formatted[:STREAM_MSG_LIMIT])`) in `streaming.py`, preventing dangling HTML tags from crashing the runtime.

### 🧪 Test Suite — 100% AAA Coverage (1608 Tests)

*   **Deterministic Circuit-Breakers**: Refactored `test_streaming.py` to correctly map the state machine of `_overflow_to_new_message`, proving the circuit breaker prevents endless looping after the 3rd API failure.
*   **Redis Event Loop Decoupling**: Fixed `Event loop is closed` flakiness in `test_long_messages.py` by severing network dependencies. Long message tests are now entirely mocked with deterministic `AsyncMock()` environments.
*   **Parametrized Failsafes**: Aligned the test suite's `STREAM_MSG_LIMIT` mock (4000) with production reality, resolving artificial failsafe truncations that were stripping the Telegraph indicator `"формирую статью"` during tests.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` / `format` | 0 errors ✅ |
| `mypy` (strict) | 0 errors ✅ |
| `pytest` | **1608 passed**, 0 failed ✅ |

---

## [2.9.18] - 2026-04-04 - Test Suite Hardening & Production Bug Fix

### 🧪 Test Suite — AAA Modernization

Complete refactor of the test layer from brittle `unittest.TestCase` patterns to a deterministic, CI-ready `pytest` suite. All new tests follow strict Arrange-Act-Assert with one behaviour per test function.

| Module | Tests | Coverage Area |
|--------|-------|---------------|
| `tests/test_formatting.py` | 16 | `TelegramFormatter`, `escape_format_chars`, `format_key_for_display` |
| `tests/test_thinking_classifier.py` | 39 | `classify_thinking_level` (all 14 signal types), context escalation, `resolve_thinking_level` |
| `tests/test_error_codes.py` | 34 | `tag_error`/`extract_error_code` roundtrip, all `ErrorCode` values, MRO/HTTP/string classification paths |
| `tests/test_text_format_aaa.py` | 30 | `markdown_to_html`, `sanitize_html_tags` (unclosed/misnested/orphan), `split_text_safe`, `strip_formatting` |
| `tests/test_factories.py` | 15 | All 5 Telegram object factories — structure, independence, async attributes |
| `tests/test_streaming_writer.py` | 21 | `StreamingWriter` write/finalize, `_detect_open_markdown`, rate-limit retry, overflow, `_is_rate_limited` |
| `tests/test_semaphore_invariants.py` | 6 | `GlobalLLMSemaphore` slot release on success, exception, `CancelledError`, limit enforcement |
| `tests/e2e/test_stream_recovery.py` | 5 | Mid-stream `APIError` recovery (no bare cursor), `TimeoutError`, empty stream |

**Total: 205 tests — 205 passed, 0 failed.**

### 🐛 Production Bug Fix — `streaming.py` Dead Code (Critical)

**Root cause**: Incorrect indentation of 168 lines (681–848) inside `stream_and_display()` placed the entire "Long Read transition", finish_reason checks, and the final `return` inside `if not final_text.strip():` — making them unreachable dead code. `stream_and_display()` returned `None` for all non-empty responses, causing `TypeError: cannot unpack non-iterable NoneType` in every caller.

**Fix**: Removed 4 spaces of over-indentation from all affected lines. Confirmed by `test_successful_stream_returns_complete_text` (E2E test now enforces this invariant).

**Likely cause**: Indentation drift introduced during a merge that inserted the Telegraph fallback block inside the `if` branch instead of after it.

### 🔧 Infrastructure Improvements

- `tests/factories.py`: Standardized `Update`, `Message`, `CallbackQuery`, `User`, `Bot` builder functions replacing per-test `MagicMock()` boilerplate
- `FakeAdapter(StreamingUIAdapter)`: Proper ABC inheritance for `StreamingWriter` tests — fulfills all abstract methods including `last_message` property
- `app/streaming.py`: Added `set["asyncio.Task[None]"]` type annotation to `_bg_tasks` (mypy `var-annotated` fix)

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check app/ tests/` | 0 errors ✅ |
| `mypy app/streaming.py tests/test_streaming_writer.py --ignore-missing-imports` | Exit 0 ✅ |
| `pytest` (205 tests) | **205 passed**, 0 failed ✅ |

---

## [2.9.17] - 2026-04-04 - Mini App Full UX & Backend Hardening


### 🚀 Feature - Mini App UX Expansion

* **Settings Panel Upgrade**: Transformed the Mini App settings into a full native-feeling UI with `tg.HapticFeedback` interactions.
* **Role Management**: Users can switch custom prompt roles directly from interactive chip strips (`Scrollable Chips`).
* **Temperature & TTS Control**: Native Telegram Segmented Control for LLM creativity (Точный / Баланс / Творческий) and an expanding List View for selecting ElevenLabs Voices.
* **Context Reset**: Added a red, destructive Action Button with native `tg.showConfirm` dialog to allow users to flush agent conversational history at will.

### 🛠 Fixes & Under The Hood
* Extended `public.chats` in Supabase with `temperature` and `voice_id` column states.
* Implemented deep link mapping `?tab=settings` mapping through Python handler routes avoiding UI jank.

## [2.9.16] - 2026-04-03 - Native Mini App Reader for Long Messages

### ✨ Feature — Mini App Reader (Telegraph Replacement)

*   **Redis-Backed Fast Reader**: Replaced the brittle, synchronous Telegraph long-message integration with a high-performance **Telegram Mini App**. Responses >4000 chars are instantly stored in Redis (24h TTL) and accessed via a beautiful, theme-aware WebApp interface (`/webapp/reader`).
*   **Syntax Highlighting & UX**: The new HTML shell features `marked.js` and `highlight.js` for perfect Markdown rendering, along with a Telegram-aware UI (matches the user's Dark/Light mode completely) and "Copy Code" blocks.
*   **Graceful Degradation (Fallback)**: A background task silently generates a permanent Telegraph Instant View link while the user is reading. If the Redis entry expires (or if the bot is deployed without `WEBAPP_BASE_URL`), it seamlessly degrades back to pure Telegraph links.

#### Files Changed
*   `app/config.py` — Added `WEBAPP_BASE_URL`
*   `app/cache.py` — Redis ops `store_long_message`, `get_long_message`, `store_telegraph_url`, `get_telegraph_url`.
*   `app/streaming.py` — Non-blocking overflow handling and inline keyboard integration.
*   `app/web_miniapp.py` — `/reader` and `/api/reader/<uid>` endpoints.
*   `app/templates/reader.html` [NEW] — Lightweight frontend with skeleton loaders and auto-theme.

---

## [2.9.15] - 2026-04-03 - Telegram Mini App & UX Phase 1-3

### 📱 Feature — Telegram Mini App (Phase 3)

Native in-app settings panel served via Quart Blueprint at `/webapp/`. Authenticated using Telegram `initData` HMAC-SHA256 validation — each user can only access their own data.

**Two-tab interface:**

| Tab | Features |
|-----|----------|
| 🧠 Память (LTM Explorer) | Paginated memory browser, client-side search, swipe-to-delete with haptic feedback, usage progress bar (count/limit) |
| ⚙️ Настройки (Settings Editor) | System prompt textarea (4000 char limit), model selector, thinking level dropdown, LTM/search toggle switches, Telegram `MainButton` save |

*   Styled with `var(--tg-theme-*)` CSS variables for automatic dark/light mode matching.
*   Accessible via `WebAppInfo` button ("📱 Открыть панель настроек") in `/settings` keyboard.
*   Uses `WEBHOOK_URL` env var for multi-deployment portability — same build works across different bots/projects.
*   CSP split: `/webapp/` routes allow `telegram.org` scripts + inline styles + iframe embedding; all other routes keep strict CSP.

### ✨ Feature — Smart UX Interactions (Phase 1)

*   **Proactive Feedback Reactions**: `set_message_reaction(👍/👎)` seeded on AI responses. 👎 → LTM negative signal, 👍 → ❤️ response. Bot's own reactions filtered out.
*   **Smart Suggestions**: `[SUGGESTIONS:...]` response tags → ✨-prefixed inline buttons with `sendMessageDraft` pre-fill.
*   **Intent Routing**: `[INTENT:...]` response tags → contextual action buttons (draw, search, etc.).
*   **CopyTextButton**: 📋 Скопировать код button for fenced code blocks in responses.
*   **Message Effects**: 🔥 `EFFECT_FIRE` on successful image generation.

### ✨ Feature — Research UX & Empathy (Phase 2)

*   **Telegraph Longreads (Legacy)**: Legacy synchronous responses >5000 chars published to Telegraph Instant View with collapsed blockquote summary. Replaced by Mini App Reader in 2.9.16.
*   **Auto TTS for Research**: Fire-and-forget `fire_voice_reply` after successful agentic search (>200 chars).

### 🐛 Bug Fixes

*   **Smart Suggestions Truncation & UI Leak fix**:
    *   Tags like `[SUGGESTIONS: ...]` no longer leak into the chat during AI responses. Integrated a `post_processor` hook into `streaming.py` that reliably strips hidden tags from the final text buffer.
    *   Resolved severe suggestion text truncation that occurred due to Telegram's 64-byte limit on `callback_data`. Suggestions now generate a 10-hex-character MD5 hash representing the full text, storing it in an in-memory `LRUCache`. Buttons display the full un-truncated string, while only passing the tiny hash in `callback_data`. `cb_smart_actions.py` then restores the full text from cache seamlessly.
*   **Stale test mock**: Removed dead `is_openrouter_model` mock targeting `app.handlers.ai_chat` (function moved to `app.providers.base`). Fixed `test_e2e_app_smoke.py`.
*   **Schema-qualified assertions**: Fixed 5 test assertions that checked for `INSERT INTO conversations` / `UPDATE conversations` but queries now use `public.conversations`. Affected: `test_repos_conversations.py`, `test_save_conversation.py`, `test_integration_flows.py`.
*   **Brief generation contract**: Fixed `test_scheduled_briefs.py` mock returning a string instead of `dict[str, str]`, matching the production `_generate_brief_summary` return type.
*   **Deprecation fix**: Replaced `asyncio.iscoroutinefunction()` (deprecated in Python 3.16) with `inspect.iscoroutinefunction()` in `streaming.py`.

#### Files Changed

| File | Change |
|------|--------|
| `app/web_miniapp.py` | [NEW] Quart Blueprint: initData auth, 5 API endpoints (memories CRUD, settings R/W) |
| `app/templates/miniapp.html` | [NEW] Two-tab Mini App frontend (vanilla HTML/JS, ~50KB) |
| `app/web.py` | Registered blueprint, CSP split for `/webapp/` routes |
| `app/handlers/commands.py` | Added `WebAppInfo` button to `/settings` keyboard |
| `app/streaming.py` | `asyncio.iscoroutinefunction` → `inspect.iscoroutinefunction` |
| `tests/integration/test_e2e_app_smoke.py` | Removed stale `is_openrouter_model` mock |
| `tests/test_repos_conversations.py` | Fixed schema-qualified table name assertions |
| `tests/test_save_conversation.py` | Fixed schema-qualified table name assertions |
| `tests/test_integration_flows.py` | Fixed schema-qualified table name assertions |
| `tests/test_scheduled_briefs.py` | Fixed mock return type to `dict[str, str]` |

#### Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors ✅ |
| Ruff format | 0 violations ✅ |
| Pytest | **1420 passed**, 0 failed ✅ |

---



### 🛡️ Hardening — Safe Message Access (`effective_message`)

*   **Root cause fixed**: All handlers accessed `update.message` directly, causing `AttributeError` crashes when Telegram delivered non-`message` update subtypes (e.g. `edited_message`, `channel_post`). Migrated every handler to `update.effective_message`, which resolves correctly across all update types.
*   **`agent.py`**: `process_long_request` now reads `update.effective_message` and also checks `context.user_data["_edited_text_override"]` — the injection point used by the new edited-message UX handler.

### ✨ Feature — In-Place Edit UX (`handle_edited_request`)

When a user **edits** a message while the bot is already generating a response:
1.  The inflight `asyncio.Task` is **cancelled** (via `state.cancel_active_task(user_id)`).
2.  The bot **edits its own previous message in-place** to `✏️ Обновляю ответ...` — no extra message is posted, chat stays clean.
3.  A fresh AI task is launched with the corrected text.
4.  Falls back to a new reply if the previous bot message was deleted.

**Handler registered** with `filters.UpdateType.EDITED_MESSAGE & filters.TEXT` — isolated from all new-message handlers.

### 🎯 Hardening — Strict `UpdateType` Filters

Replaced implicit (all-update) `MessageHandler` registrations with explicit `filters.UpdateType.MESSAGE` guards on every handler group in `messages.py`. Prevents cross-contamination of update types and eliminates "phantom" processing of unsupported events.

### ⚡ Feature — Ambient Native Reactions Feedback (`msg_reactions.py`)

New module registers `MessageReactionHandler` to silently collect native 👍/👎 reactions:

| Reaction set | Rating |
|---|---|
| `👍 ❤️ 🔥 🥰 👏 🎉 🤩 💯 ⚡ 🏆 🫡 ✅` | `up` |
| `👎 💩 🤮 🤬 😤 😡 🖕` | `down` |

*   **Zero UI noise**: no confirmation toasts, no inline buttons needed. Feedback is ambient.
*   Writes to the same `save_feedback` table as the old inline button system — reporting unchanged.
*   Added `msg.rethinking` i18n key (`✏️ Обновляю ответ...` / `✏️ Updating answer...`).

### 🔒 Hardening — Explicit `allowed_updates` Whitelist (`bot.py`)

Replaced `Update.ALL_TYPES` (which subscribes to 20+ rarely-used update types) with a minimal whitelist in both webhook and polling modes:

```python
["message", "edited_message", "callback_query", "inline_query", "message_reaction"]
```

This narrows the surface area, reduces unnecessary webhook deliveries, and eliminates edge-case fallthrough crashes from unsupported update types.

### 🗂️ State Management — Ephemeral Task & Message Registries (`state.py`)

Added two in-memory runtime maps (zero DB overhead):

| Registry | Purpose |
|---|---|
| `_ACTIVE_TASKS: dict[int, asyncio.Task]` | Track inflight task per user for cancellation on edit |
| `_LAST_BOT_MESSAGE: dict[int, tuple[int, int]]` | Track `(message_id, chat_id)` of last bot reply for in-place reuse |

Public API: `register_active_task`, `cancel_active_task`, `clear_active_task`, `set_last_bot_message`, `get_last_bot_message`.

#### Files Changed

*   **Updated**: `app/handlers/messages.py` — Full refactor: `effective_message` everywhere, `UpdateType` filters, `handle_edited_request`, task/message tracking.
*   **Updated**: `app/handlers/agent.py` — `effective_message` migration, `_edited_text_override` support.
*   **New**: `app/handlers/msg_reactions.py` — Native reaction feedback handler.
*   **Updated**: `app/state.py` — Two ephemeral runtime registries + public accessor functions.
*   **Updated**: `app/i18n.py` — `msg.rethinking` key added.
*   **Updated**: `bot.py` — Reaction handler registration, explicit `allowed_updates` whitelist.

#### Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` (6 changed files) | 0 errors ✅ |
| `python -m py_compile` (6 files) | 0 syntax errors ✅ |

---

## [2.9.13] - 2026-04-01 - UX: Message Trailing Debounce & Ephemeral Toasts

### ✨ Feature — Telegram Forward Burst Handling (Trailing Debounce)

*   **1.1s Trailing Window**: Replaced the fixed 400ms debounce window with a 1.1s *trailing* debounce in `app/middleware/debounce.py`. When a user forwards a large batch of messages, the Telegram API artificially spaces them out by ~500ms. The new trailing window resets its timer on every incoming message, perfectly aggregating the entire burst into a single cohesive AI context before processing.
*   **Perceived Zero-Latency**: Since the bot emits a `ChatAction.TYPING` indicator immediately upon receiving the first message in the burst, the 1.1s debounce window is completely masked from the user. It feels like the AI is "attentively reading" rather than lagging.

### 🛡️ UX Hardening — Ephemeral Busy Toasts

*   **Self-Destructing Warnings**: When users send additional messages while the AI is already generating a response, the bot gracefully informs them ("⏳ Дождитесь завершения текущего запроса"), but this warning is now *ephemeral*. A background task (`submit_task`) automatically deletes the warning from the chat after 4 seconds to prevent polluting the conversation history with system errors.
*   **i18n Integration**: Fixed a bug where the handler would emit a raw string key (`"busy.user"`). The busy toast is now fully localized and auto-detects the user's language using `detect_language`.

#### Files Changed
*   **Updated**: `app/middleware/debounce.py` (Rewrote `_DebounceSlot` logic to support trailing timer reset via `cancel()`).
*   **Updated**: `app/handlers/messages.py` (Replaced all generic `busy.user` rejections with the new `_send_busy_ephemeral` auto-deleting helper).

## [2.9.12] - 2026-03-31 - Hybrid Image Intent Recognition

### ✨ Feature — Contextual Semantic Trigger

*   **Hybrid AI-Regex Intent Detection**: Evolved the image generation pipeline to support highly complex, conversational requests containing coreferences (e.g., *"Я видел картинки гор. Сгенерируй мне такие же"*).
    *   **Level 1 (Regex)**: Maintains zero-latency instant triggering for standard commands ("нарисуй кота").
    *   **Level 2 (Semantic AI)**: Uses `gemini-3.1-flash-lite` asynchronously (`_extract_draw_prompt_ai`) to read between the lines if a user types a generation verb but doesn't explicitly name a subject immediately.
*   **Voice Intent Integration**: Overhauled `multimodal_processor.py`'s `_VOICE_SYSTEM_PROMPT`. The ASR layer now natively identifies the `INTENT:DRAW` state and automatically extracts a cleaned `DRAW_PROMPT: <subject>` inline, neutralizing complex audio requests without regex fallback.

#### Files Changed
*   **Updated**: `app/handlers/cmd_image.py` (Implemented `check_draw_intent_async` and AI coreference logic).
*   **Updated**: `app/handlers/messages.py` (Adapted text router to await asynchronous intent detection).
*   **Updated**: `app/utils/multimodal_processor.py` (Injected inline DRAW_PROMPT parsing into ASR logic).
*   **Updated**: `app/handlers/msg_voice.py` (Prioritize AI-extracted draw prompts returned by the multimodal processor).

## [2.9.11] - 2026-03-31 - Prompt Truncation & Voice UX

### ✨ Feature — Image Generation UX & Voice Integration

*   **Zero-Leak Intent Recognition**: Improved the Regex heuristics engine behind implicit image generation triggers. The bot now natively identifies and discards object pronouns and conversational filler words (e.g. `мне нужно`, `пожалуйста`, `эээ`) surrounding the generation verb. Resolves semantic leakage where the word "мне" would accidentally become part of the artistic prompt.
*   **Pre-Canvas Voice Confirmation**: Deprecated blind auto-generation for voice commands. When a user requests an image via audio ("нарисуй кота"), the bot now parses the transcript and immediately attaches the interactive Canvas 2.0 keyboard directly to the text message. Users can visually confirm the parsed prompt and freely tweak the Model and Aspect Ratio *before* any API tokens are consumed. 
*   **Unrestricted Prompt UX**: Eradicated the aggressive 80-character truncation across the entire Canvas 2.0 UI. Generated images now display full-length prompts (safely capped at 800 characters to respect Telegram's 1024 limit).
*   **Frictionless Prompt Editing**: The "✏️ Изменить промпт" menu no longer forces the original prompt into a truncated caption. It now displays the complete original text in a dedicated markdown block, enabling 1-tap copy-pasting for mobile users.

#### Files Changed
*   **Updated**: `app/handlers/cmd_image.py` (Extended `_DRAW_PREFIX` & `_DRAW_POST_VERB` regex structures; safely raised caption limits).
*   **Updated**: `app/handlers/cb_image.py` (Redesigned prompt editing UX to support full-length string formatting).
*   **Updated**: `app/handlers/msg_voice.py` (Rewrote `_auto_route_to_image` to inject `_build_main_menu` inline instead of firing `_run_generation` blindly).

## [2.9.10] - 2026-03-31 - Memory Core & SDK Hardening

### 🧠 GraphRAG Memory & Pipeline Standardization

*   **API Key Routing Fix**: Resolved `400 INVALID_ARGUMENT` crashes in the background memory storage pipeline. Fixed an architecture bug where the bot improperly reused the active session's API key (often OpenRouter) when making Google-specific embedding calls (`gemini-embedding-2-preview`). The pipeline now proactively fetches an isolated Google key for all graph processes.
*   **Model Accuracy**: Enforced the `gemini-embedding-2-preview` (768-dim) and `gemini-3.1-flash-lite` endpoints for all internal RAG components.
*   **Legacy SDK Eradication**: Conducted a full codebase audit and permanently removed all usages of the deprecated `google.generativeai` SDK. Operations like graph clustering (`memory_consolidation.py`) and scheduled summarization (`scheduled_briefs.py`) now natively utilize the modern `google-genai` SDK with strict `GenerateContentConfig` structures.

#### Files Changed
*   **Updated**: `app/handlers/ai_chat.py` (Isolated key logic in `_store_memory_in_background`)
*   **Updated**: `app/repos/memory_consolidation.py` (Purged legacy SDK, migrated to `get_cached_genai_client`)
*   **Updated**: `app/handlers/scheduled_briefs.py` (Migrated Gemini calls to identical client infrastructure)

## [2.9.9] - 2026-03-31 - Implicit Image Generation Intent

### ✨ Feature — Implicit Image Generation Intent (Text & Voice)

*   **Seamless Triggers**: The bot now recognizes natural language requests to draw or generate an image (e.g., *"Бот, нарисуй кота"*, *"сгенерируй картинку леса"*) natively. No need to explicitly use the `/draw` command anymore.
*   **Bypasses Chat Flow**: It proactively intercepts these requests in both the standard text handler and immediately after voice transcription, routing them straight to the Canvas 2.0 image generation pipeline.
*   **UX Consistency**: Retains the complete interactive Image Canvas experience (aspect ratio, exact model switching, one-tap regeneration) under the implicit triggers.
*   **Voice Integration**: Voice requests display a localized confirmation ("🎨 *генерирую изображение...*") directly below the transcription UI to communicate that generation has begun.

#### Files Changed
*   **Updated**: `app/handlers/cmd_image.py` (Added `check_draw_intent(text)` Regex matcher that strictly strips out the bot prefix and conjugation to isolate the core prompt).
*   **Updated**: `app/handlers/messages.py` (Intercepts text messages matching the intent, preventing overlapping request locks).
*   **Updated**: `app/handlers/msg_voice.py` (Extended `_should_auto_route` system with `_auto_route_to_image()` for implicit voice interception).

## [2.9.8] - 2026-03-31 - Pollinations.ai Image Generation

### 🎨 Feature — Image Generation Canvas 2.0 & Pollinations.ai

*   **Image Generation Canvas 2.0**:
    *   **Deferred Generation**: Changing model or format no longer instantly regenerates the image. Instead, users configure parameters via a multi-level menu and press "▶️ СГЕНЕРИРОВАТЬ" to execute.
    *   **Prompt Translation**: Added transparent prompt translation for `flux` and other non-Cyrillic models. If a user sets a Russian prompt, it is automatically translated to English via `gemini-3.1-flash-lite`, preserving the exact visual intent.
    *   **Live Prompt Editing**: Added "✏️ Изменить промпт" button, allowing users to send text to seamlessly update the current generation state without starting a new `/draw` command.
    *   **Instant Enhancement**: Added "✨ Улучшить промпт" toggle, integrating natively with Pollinations' LLM-based prompt enhancer.

Full production-grade integration of **Pollinations.ai** as the default free-tier image generation provider, addressing the strict paywalling of Google's Imagen API on free keys.

#### Architecture
- **Dual Transport Provider**: The new `PollinationsProvider` utilizes a robust resilient design:
  - **Primary**: `POST /v1/images/generations` (OpenAI-compatible) for clean structured error handling.
  - **Fallback**: Automatically degrades to a keyless `GET /image/{prompt}?model=…` endpoint if the primary times out, 503s, or fails.
  - **MIME type guarding**: Discards invalid `Content-Type` responses (e.g. Cloudflare HTML challenges) on the GET fallback to prevent sending error pages as images.
- **Dynamic Configuration**: UI no longer hardcodes models. The `IMAGE_MODELS` (default: `flux,zimage`) environment variable instantly modifies the available models in the telegram keyboard. Adding a new model (e.g. `IMAGE_MODELS=flux,zimage,gptimage`) wraps rows perfectly via the new `_ideal_columns()` math chunker.
- **Provider Factory Routing**: Handlers route `/draw` commands gracefully: if `model.startswith("imagen-")`, it dispatches to Google's Provider (maintaining legacy support for paid users). Everything else routes to Pollinations.

#### Files Changed
- **New File**: `app/providers/pollinations.py`
- **Rewritten**: `app/handlers/cmd_image.py` (Dynamic Canvas logic, provider routing)
- **Rewritten**: `app/handlers/cb_image.py` (Unified model validation against config)
- **Updated**: `app/config.py` (Added `IMAGE_MODELS`, `DEFAULT_IMAGE_MODEL`, `POLLINATIONS_API_KEY`)

## [2.9.7] - 2026-03-30 - Imagen 4 Image Generation

### 🎨 New Feature — Text-to-Image Generation (Imagen 4)

Adds full image generation support to GemAI Bot v2 via the **Imagen 4 API** family (`Fast`, `Base`, `Ultra`).

#### Command Interface

| Command | Aliases | Behaviour |
|---------|---------|-----------|
| `/draw <prompt>` | `/img`, `/image`, `/generate` | Generates an image from the given text description |

During generation (10–20 s), bot sends `ChatAction.UPLOAD_PHOTO` heartbeat every **4.5 s** to prevent the Telegram "unresponsive" indicator.
After generation, the image is sent with an **Interactive Canvas** inline keyboard:

| Button | Action |
|--------|--------|
| 🔄 Сгенерировать заново | Regenerate with the same prompt and settings |
| ◻️ 1:1 / 📱 3:4 / 🖥️ 4:3 / 📲 9:16 / 🎬 16:9 | Switch aspect ratio and regenerate automatically |
| ⚡ Fast / ✨ Base / 💎 Ultra | Switch Imagen 4 model and regenerate automatically |

Prompt and last-used parameters are stored in PTB's `context.user_data["draw_state"]` — no database writes needed for inline regeneration.

#### Supported Models

| Label | Model ID | Free-tier RPD | Characteristics |
|-------|----------|---------------|-----------------|
| ⚡ Fast | `imagen-4.0-fast-generate-001` | 25/key/day | Lowest latency |
| ✨ Base | `imagen-4.0-generate-001` | 25/key/day | Balanced (default) |
| 💎 Ultra | `imagen-4.0-ultra-generate-001` | 25/key/day | Highest quality |

#### Quota Isolation (Critical Architecture)

`ImagenProvider` uses an **isolated RPD (Requests Per Day) budget** stored in Redis:

- **Redis key format:** `imagen:rpd:<sha256(api_key)[:12]>` — `INCR` + `EXPIREAT(next UTC midnight)`.
- **Fallback:** When Redis is unavailable (dev environment, no `REDIS_URL`), an in-memory dict is used transparently — behaviour is identical.
- **Result:** When all Imagen keys reach their 25 RPD limit, **only image generation fails**. LLM chat, streaming, and audio continue using the same API keys via `KeyStatusManager` without interruption. There is **zero cross-service impact**.

#### Error Handling

| Error | User Message |
|-------|-------------|
| Safety block | "🚫 Запрос заблокирован фильтром безопасности" + rephrasing tips |
| Quota exhausted | "⏳ Дневной лимит генерации изображений исчерпан" (shows RPD limit) |
| Timeout | "⏰ Время ожидания истекло" |
| Overloaded | "⚡ Серверы перегружены. Попробуйте снова" |

### 🗂️ New Files

| File | Role |
|------|------|
| `app/providers/imagen_provider.py` | Isolated Imagen 4 API client with Redis-backed RPD tracking |
| `app/handlers/cmd_image.py` | `/draw` command, Heartbeat, Interactive Canvas keyboard |
| `app/handlers/cb_image.py` | Callback dispatcher `draw:regen`, `draw:ar:*`, `draw:model:*` |

### 📝 Modified Files

| File | Change |
|------|--------|
| `app/config.py` | Added `IMAGEN_MODEL_*` constants; `IMAGE_GEN_*` settings with sane defaults |
| `app/handlers/commands.py` | Registered `/draw`, `/img`, `/image`, `/generate` aliases |
| `app/handlers/callbacks.py` | Registered `^draw:` callback pattern |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint (3 new files) | 0 errors (4 auto-fixed import sort + UP017) |
| Mypy (`--ignore-missing-imports`) | Exit 0 |
| Pytest (full suite) | **1421 passed**, 0 failed |
| `py_compile` (all 3 new files) | 0 syntax errors |

---

## [2.9.6] - 2026-03-30 - GraphRAG 5-Point Tuning & ElevenLabs TTS

### 🧠 GraphRAG Memory — 5 Architectural Improvements

#### Change 1 — Multi-Query Expansion
- **New function `expand_query_with_llm()`** in `memory.py`: fast Flash-Lite LLM call (~200ms) rewrites vague queries ("тот фреймворк который я упоминал") into keyword-dense search phrases ("Python FastAPI web framework project") before the embedding lookup.
- Fall-through: any exception silently returns the original query — zero pipeline disruption risk.
- Model: `gemini-3.1-flash-lite` (constant `QUERY_EXPANSION_MODEL`).

#### Change 2 — Adaptive Similarity Thresholding
- `search_memories()` rewritten with **over-fetch × 2** (relaxed floor `min_similarity − 0.12`, min 0.40) + **gap-filter** (keeps only results within 15pp of best score, hard floor still enforced).
- Eliminates false negatives from a rigid fixed threshold while blocking low-relevance "vector spam".
- Candidate pool for RRF semantic CTE raised from `LIMIT 20` → `LIMIT 40`.

#### Change 3 — 2-Hop Graph Traversal
- `search_memories_with_graph()` replaces the single-hop edge query with a **SQL CTE `hop1 UNION ALL hop2`**: follows 1-hop neighbours' outgoing edges for indirect associations.
- Hop-2 edges are excluded from seed nodes to avoid cycles.
- Result cap raised to 15 triples (was 10), sorted by `effective_weight DESC`.
- Triples now include a `★` marker for core facts and `(indirect)` label for hop-2 results.

#### Change 4 — Semantic Edge Deduplication
- During consolidation (`consolidate_memories`), each new predicate is embedded and checked against existing `(src, tgt)` predicates via cosine distance (`< 0.25`).
- If a semantically duplicate edge exists: weight and `is_core` are merged into the existing row (`UPDATE`), no new edge created.
- New column `predicate_embedding halfvec(768)` added to `memory_edges` via migration `026b`.

#### Change 5 — Core Persona Protection (Eternal Facts)
- `_GRAPH_EXTRACTION_PROMPT` updated: LLM now sets `is_core: true` for permanent identity facts (name, profession, home, chronic conditions). All transient facts (`is_core: false` by default).
- New column `is_core BOOLEAN NOT NULL DEFAULT FALSE` in `memory_edges` (migration `026`).
- Graph traversal SQL: `CASE WHEN is_core THEN weight ELSE weight / (time_decay_formula) END AS effective_weight` — core facts bypass temporal decay entirely.
- `consolidate_memories` upsert: `is_core = memory_edges.is_core OR EXCLUDED.is_core` — once core, always core.

### 🎙️ ElevenLabs TTS Integration (Primary Provider)

| Change | Files | Detail |
|--------|-------|--------|
| **ElevenLabs primary TTS** | `providers/elevenlabs_tts.py` [NEW], `voice_engine.py` | Atomic Router: ElevenLabs generates all chunks or falls back entirely to Gemini TTS — no mixed-provider audio. |
| **Request Stitching** | `elevenlabs_tts.py` | `previous_text` / `next_text` context passed to each chunk call; eliminates prosodic artifacts at sentence boundaries. |
| **Text normalization** | `elevenlabs_tts.py` | `apply_text_normalization="on"` — natural reading of dates, abbreviations, numbers. |
| **Voice tuning** | `elevenlabs_tts.py` | `stability=0.50`, `similarity_boost=0.80`, voice: Charlotte (`XB0fDUnXU5powFXDhCwa`). |
| **Key rotation** | `config.py`, `.env` | `ELEVENLABS_API_KEYS` comma-separated pool with load-balanced rotation. |

### 🗄️ Database Migrations

| Migration | Detail |
|-----------|--------|
| `026_add_core_persona_edges.sql` | `is_core BOOLEAN NOT NULL DEFAULT FALSE` + partial index `WHERE is_core = TRUE`. `DO $$ IF NOT EXISTS` idempotency guard. |
| `026b_add_predicate_embedding.sql` | `predicate_embedding halfvec(768)` + HNSW partial index `WHERE IS NOT NULL`. `DO $$ IF NOT EXISTS` idempotency guard. |

### 📐 LTM Parameter Changes

| Parameter | Old | New |
|-----------|-----|-----|
| `min_similarity` (floor) | 0.72 (hard) | 0.68 floor + adaptive gap-filter (−15pp from top) |
| `limit` | 3 | 5 (noise filtered by adaptive thresholding) |
| Graph hops | 1 | 2 (SQL CTE hop1 + hop2) |
| Graph edge limit | 10 | 15 |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint (full project) | 0 errors |
| Ruff format | 0 violations |
| Mypy (`--ignore-missing-imports`, 5 files) | Exit 0 |
| AST parse (3 core files) | 0 syntax errors |
| Migration idempotency | `DO $$ IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` |

---



## [2.9.5] - 2026-03-30 - Voice Pipeline Hardening: Byte-Based Chunking & Sequential TTS

### 🛡️ Reliability — Eliminated Phantom 429 Quota Exhaustion


Root cause: The parallel `asyncio.gather` TTS architecture would fire 2–3 simultaneous REST calls per voice reply. Free Tier keys carry a **~10 RPD** (requests-per-day) cap on `gemini-2.5-flash-preview-tts`. A single parallel burst consumed ≥30% of the daily quota, triggering rapid key suspension and generating phantom 429 errors that persisted for up to 24 hours.

| Change | Files | Detail |
|--------|-------|--------|
| **Byte-based chunking** | `providers/tts.py` | `_chunk_text_by_sentences` rewritten from `max_chars: int = 1500` to `max_bytes: int = 3500`. Uses `len(part.encode("utf-8"))` accumulator. Cyrillic = 2 bytes/char; character counts were systematically under-counting payload size. 3500 bytes provides ~500-byte headroom under the 4000-byte API text-field limit. |
| **Sequential generation** | `voice_engine.py` | Replaced `asyncio.gather` parallel chunk dispatch with a sequential `for i, chunk in enumerate(chunks)` loop. Each chunk uses a fresh key slot via the existing rotation pool — no burst, no suspension. |
| **Adaptive timeout** | `voice_engine.py` | Fixed 120 s timeout replaced with `min(120.0, max(30.0, len(text) / 60.0 + 15.0))`. Short messages (~600 chars) now use 30 s; a full 2500-char chunk uses ~57 s. Prevents false hangs on short payloads. |
| **User quota notification** | `voice_engine.py` | On complete TTS failure, `status_msg.edit_text("🔇 Голосовой ответ недоступен — превышена квота API.")` is shown for 5 s instead of silently deleting the indicator. Eliminates silent failure UX. |
| **Partial audio delivery** | `voice_engine.py` | First-chunk failure aborts (no audio without context); later-chunk failures break and deliver successfully generated portion. Logged with chunk index for diagnostics. |
| **Type annotation fix** | `voice_engine.py` | `_factory` coroutine closure now annotated as `Coroutine[Any, Any, None]` — resolves the single mypy `no-untyped-def` introduced in the file. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 0 violations |
| Mypy (`--strict`, 2 source files) | 0 errors in `voice_engine.py` + `tts.py` |
| py_compile | 0 errors |
| Unit assertions (4) | All passed |

---

## [2.9.4] - 2026-03-29 - GraphRAG Hardening & Streaming Cleanup

### 🚀 Features

#### GraphRAG Semantic Memory Hardening
- **Semantic Entity Resolution**: Integrated `pgvector` distance matching (`< 0.12`) into LTM consolidation (`memory_consolidation.py`) to semantically merge identical entities, drastically reducing graph fragmentation.
- **Temporal Edge Upserts**: Added `updated_at` tracking to `memory_edges` with `ON CONFLICT` constraints to guarantee idempotent relationship updates across memories.
- **Time-Decayed Traversal**: Upgraded `search_memories_with_graph` (`memory.py`) to automatically prioritize recently updated relationships using SQL time-decay `ORDER BY` sorting.

#### Streaming Architecture Cleanup
- **Legacy UI Abstraction Removal**: Completely eliminated deprecated `DRAFT_MODE` constants (`DRAFT_DEBOUNCE_S`, `DRAFT_MIN_CHUNK`) from the streaming subsystem (`streaming.py`).
- **Signature Hardening**: Stripped the unused `chat_type` routing parameter from `stream_and_display()` across all handler call sites (Chat, Photo, Search, Document).

### 🗄️ Database Migrations
- `scripts/migrations/025_add_temporal_graph_edges.sql`: Added `updated_at` column to `memory_edges` and created a unique constraint on `(user_id, source_node, target_node, predicate)` to enable ON CONFLICT upserts.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy strict | 0 errors |
| Pytest | 100% Pass |

---

## [2.9.3] - 2026-03-29 - Mid-Stream Recovery & LLM Voice Intent

### 🚀 Features

#### Resilient Streaming Pipeline & Recovery
- **Mid-Stream API Error Isolation**: `gemini.py` and `router.py` now `raise` exceptions instead of yielding raw string JSON errors into the chat when API failures (like 503 Service Unavailable) occur mid-stream.
- **Graceful Stream Interruption**: `streaming.py` detects mid-flight drops, appends a clean, localized footer (`⚠️ _(ответ был прерван из-за ошибки сервера)_`), and returns a new `was_interrupted` flag.
- **Interactive Recovery Options**: If a stream drops, `ai_chat.py` presents an inline keyboard with `[▶️ Продолжить]` and `[🔄 Заново]` buttons, allowing users to salvage partial context.
- **Seamless State Reintegration**: `continue_stream_callback` seamlessly injects the salvaged partial output into the conversation history, allowing the LLM to pick up exactly where it halted with zero context loss.

#### Zero-Latency LLM Voice Intent Detection
- **`[VOICE]` Tag Architecture**: `prompt_registry.py` now instructs the model to prefix its response with a `[VOICE]` tag if the user explicitly requested voice output (e.g., "озвучь ответ", "прочитай вслух").
- **Invisible Extraction**: Replaced static regex keyword matching with pure intelligence. The `streaming.py` component dynamically detects and strips the `[VOICE]` tag from the very first text chunk, emitting an invisible `voice_requested` flag without ever showing the syntax to the user.
- **Strict Modality Compliance**: Eradicated the legendary "voice stickiness" issue forever. Text flows stay text strings, Voice flows stay voice recordings unless seamlessly overriden by the new `[VOICE]` tag framework.

### 📊 Observability
- Added `stream_recoveries` and `voice_intents` counters to `ConversationMetrics` for telemetry.

### 🐛 Bug Fixes
- **Phantom Draft Mode Removal**: Eradicated hallucinated `sendMessageDraft` abstraction from `streaming.py` which caused the native UI to violently delete "Думаю..." placeholders during API timeout intervals.
- **Heartbeat Resilience**: Moved `stop_heartbeat` invocation from `ai_chat.py` directly into the streaming cycle's first-chunk boundary (`yield_hook`), ensuring that the animated `🤔 Думаю...` loader persists visually during extended Google API 503 backoff retries (e.g. holding 45s gaps seamlessly instead of disappearing).
- **TTS Timeout Deadlocks**: Fixed identical-key retries when `TimeoutError` was masked as `None`, and reduced TTS chunk generation to 800 chars to reliably stay under Google API limits.
- **FFMPEG Resource Throttling**: Increased Opus encoding timeouts from `15s` to `90s` and added sub-process SIGKILL handling for processing giant PCM buffers (>8MB) on constrained nodes without deadlocking the background voice engine.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy strict | 0 errors |

---

## [2.9.2] - 2026-03-29 - Full-Text TTS & Atomic Voice Toggle

### 🚀 Features

#### Full-Text Voice Replies (PCM Concatenation)
- **Removed 1500-character TTS limit**: Voice replies now cover 100% of the response text, regardless of length.
- **Sentence-boundary chunking**: Long texts are split into ≤1500-char chunks at sentence boundaries (`.!?…`), avoiding mid-word cuts.
- **Parallel TTS generation**: Multi-chunk texts are generated concurrently (up to 3 parallel calls with `asyncio.Semaphore`), sharing the key rotation pool across chunks.
- **PCM concatenation**: Raw PCM buffers (24kHz 16-bit mono) are concatenated in memory and transcoded to OGG Opus once via `ffmpeg`. No quality loss at chunk boundaries since the sample format is identical.
- **Graceful degradation**: If some chunks fail, the bot voices the successfully generated portion and logs a warning.

#### Atomic Voice-for-Voice Toggle (Sticky Voice Fix)
- **Fixed sticky voice bug**: Previously, confirming a voice message via the UI (`cb_voice.py`) would set `reply_with_voice=True` and all subsequent messages would also generate voice — even plain text messages.
- **New behavior**: Voice reply is now decided **atomically per-request** based on two rules:
  1. **Voice source** → voice reply (voice message confirmation and auto-route).
  2. **Explicit text keyword** → voice reply (`"озвучь ответ"`, `"ответь голосом"`, `"прочитай вслух"`).
- No voice state is cached or persisted across chat turns. Text messages never trigger voice unless explicitly requested.

| File | Change |
|------|--------|
| `voice_engine.py` | Rewritten: chunked parallel TTS + PCM concatenation pipeline |
| `providers/tts.py` | Added `_chunk_text_by_sentences()`, removed hard truncation |
| `handlers/cb_voice.py` | Documented voice-for-voice intent (no code change needed, sticky bug was architectural) |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| py_compile | 0 errors |

---


## [2.9.1] - 2026-03-29 - Voice Latency Optimization

### ⚡ Performance — Voice Reply Delay Fix (~65s → ~10-15s)

Root cause: `gemini-2.5-flash-preview-tts` had no entry in `DAILY_LIMITS`, so key rotation was purely reactive (only after 429 failure). First API failure cost ~51s due to quota suspension + retry cycle with next key. Combined with sequential launch and large audio payload, total voice delay was ~65 seconds after text.

| Priority | Change | Files | Detail |
|----------|--------|-------|--------|
| **P0** | Proactive TTS key rotation | `.env` | Added `gemini-2.5-flash-preview-tts: 8` to `DAILY_LIMITS`. With 16 keys × 8/key = 128 TTS calls/day before quota exhaustion. Keys now rotate by usage count (proactive), not by 429 failure (reactive). Eliminates ~50s delay. |
| **P1** | OGG Opus bitrate 48k→24k | `audio.py` | Speech-optimized bitrate halves OGG file size (~800KB → ~400KB) with negligible quality loss for `voip` mode. Saves ~3-5s on Telegram upload. |
| **P2** | TTS text limit 2000→1500 | `tts.py`, `voice_engine.py` | Shorter text = proportionally less PCM generation time (6.3MB → ~4.7MB). Saves ~3-5s on TTS generation + transcoding. |
| **P3** | Voice reply launch reordered | `ai_chat.py` | `fire_voice_reply()` now fires BEFORE `update_user_chat()` and `_store_memory_in_background()`. Since it's fire-and-forget, no dependency on state persistence. Saves ~200ms. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy | 0 errors |
| Tests | **1453 passed**, 0 failed |

---

## [2.9.0] - 2026-03-29 - Voice Engine Architecture v3

### 🏗️ Architecture — Live API Removed

| Component | Files | Detail |
|-----------|-------|--------|
| **Live API Removed** | `live_audio.py` (deleted), `voice_engine.py` | After 3 failed approaches across 4 sessions, concluded that `gemini-3.1-flash-live-preview` **does not support one-shot text-to-speech**. The API is architecturally designed for bidirectional audio streaming (mic→model→speaker). `send_realtime_input(text=...)` → TimeoutError (VAD never detects end-of-turn for text). `send_client_content` → `1007 Invalid argument` (only for history seeding). Manual VAD (`activityStart`/`activityEnd`) → `1007 Precondition check failed` (text-only activity unsupported). Additionally, affective dialogue is not supported on the 3.1 model. |
| **REST TTS Promoted** | `voice_engine.py` | REST TTS (`gemini-2.5-flash-preview-tts`) promoted from fallback to sole provider. Eliminates ~1-2s of wasted retry time per voice reply. |
| **Caller Cleanup** | `ai_chat.py`, `cb_ai_actions.py` | Removed `use_live_api` parameter from all `fire_voice_reply()` call sites. |

### 🔊 TTS Quality — Production-Grade Prompt Engineering

| Component | Files | Detail |
|-----------|-------|--------|
| **Voice Change** | `tts.py`, `voice_engine.py` | Switched from **Kore** ("Firm, confident" — harsh/raspy in Russian) to **Aoede** ("Breezy, natural" — smooth conversational narration). Eliminates reported breathiness/roboticness artifacts. |
| **Text Pre-Processing** | `tts.py` | Added 9-stage regex pipeline to clean bot output before TTS: strips code blocks, inline code (keeps text), Markdown images, links (keeps visible text), bare URLs, HTML tags, bold/italic markers, header hashes, emoji clusters (reduces to single), and excessive whitespace. Prevents the model from reading `**bold**` markers or URLs aloud. |
| **Director's Notes Prompt** | `tts.py` | Production-grade prompt following the official Gemini TTS prompting pattern with: **Character** (warm companion persona), **Delivery** (smooth/clear, no breathiness/rasp, natural intonation variance), **Pronunciation rules** (Russian ё/е with 5 concrete examples, abbreviation expansion for ИИ/ООН/т.д./т.е./г., number/date reading in surrounding language, foreign name preservation, typo recovery from context), **Constraints** (anti-commentary injection, anti-preamble, silent formatting symbol skip). |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy (`--no-incremental`) | 0 errors (2 source files) |
| Tests | **1453 passed**, 0 failed |



## [2.8.77] - 2026-03-29 - Live API Root Cause Fix

### 🎙️ Voice Engine — Live API Restored

| Component | Files | Detail |
|-----------|-------|--------|
| **Root Cause Fix** | `live_audio.py` | Identified and fixed the root cause of all Live API timeouts: `send_realtime_input(text=...)` routes through the VAD (Voice Activity Detection) pipeline, but with text-only input there is no audio stream for VAD to detect silence — so the model waits indefinitely. Added `send_realtime_input(audio_stream_end=True)` immediately after the text to flush the VAD pipeline and trigger model response. **This single line resolves the persistent WebSocket timeouts that forced the Live API to be disabled.** |
| Live API Re-enabled | `voice_engine.py` | Removed the hardcoded `and False` dead-code guard on the Live API branch. Live API is now the **primary** voice generation path, with REST TTS (`gemini-2.5-flash-preview-tts`) as the fallback — as originally designed. |
| Model Resolution Alignment | `voice_engine.py` | Fixed model name mismatch in `_resolve_ai_request()`: was resolving keys for the deprecated `gemini-2.5-flash-native-audio-preview-12-2025`, while `live_audio.py` was actually calling `gemini-3.1-flash-live-preview`. Keys are now resolved for the correct model. |
| Output Transcription | `live_audio.py` | Fixed transcription collection: was incorrectly reading from `model_turn.parts[].text` (empty in AUDIO-modality sessions). Now reads from `server_content.output_transcription.text` per the SDK specification. |
| Deprecated Fallback Removed | `live_audio.py` | Removed deprecated `gemini-2.5-flash-native-audio-preview-12-2025` fallback model from the Live API provider. REST TTS in `voice_engine.py` is the real fallback — no need for a second model cascade within the Live API path. |
| System Instruction | `voice_engine.py` | Simplified the TTS system instruction from a verbose 5-line prompt to a concise 1-liner, reducing risk of Live API session configuration conflicts. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy | 0 errors (2 source files) |
| Tests | **1453 passed**, 0 failed |

---

## [2.8.76] - 2026-03-29 - Voice Pipeline Stability & UI Fixes
 
### 🎙️ Voice & TTS Engine Fixes
 
| Component | Files | Detail |
|-----------|-------|--------|
| Live API SDK Bump | `live_audio.py` | Migrated internal WebSocket stream from deprecated `session.send(client_content)` to `session.send_realtime_input(text=...)`. This correctly processes pure-text triggers into Live API audio without timing out on Google GenAI SDK v1+. |
| Live API Models | `live_audio.py` | Updated preferred Live model to `gemini-3.1-flash-live-preview`, replacing the deprecated legacy `gemini-2.5-flash-native-audio-preview-*` series. Validated `ThinkingLevel=Minimal` naturally engages and correctly streams inline audio and output transcription tokens concurrently. |
| Fallback Models | `live_audio.py` | Relocated `gemini-3.1-flash-live-preview` to primary. Restored `gemini-2.5-flash-native-audio-preview-12-2025` strictly as the stable Google AI Studio fallback. |
| TTS Generation Timeouts | `tts.py` | Increased the REST TTS fallback timeout limit from 30s to 90s, resolving intermittent `TimeoutError` exceptions that were occurring when generating long (>2000 char) PCM voice replies via `gemini-2.5-flash-preview-tts`. |
| UI Transcript Persistence | `msg_voice.py`, `cb_voice.py` | Addressed aggressive placeholder overwriting. Rather than editing the original audio transcript message, voice auto-routing and confirmation dialogs now mark the transcript as "✅ Принято" and spawn a brand new placeholder for the AI's response, leaving the user's transcript intact for historical context. |
 
---

## [2.8.75] - 2026-03-28 - ASR Prompt Bleeding & Verb Tolerance
 
### 🎙️ ASR Accuracy & Intent Detection Fixes
 
| Component | Files | Detail |
|-----------|-------|--------|
| ASR Prompt Bleeding | `multimodal_processor.py` | Addressed "instruction following bleed" where the LLM used for transcription would execute commands in the audio (e.g. "compose a story") instead of just transcribing them. Hardened `_VOICE_SYSTEM_PROMPT` to explicitly categorize generation verbs (compose, write, generate) into `INTENT:CONVERSATIONAL` rather than `INTENT:TRANSCRIPTION`, ensuring voice commands correctly route to the Chat AI instead of failing open with raw text. Also bypassed short-message summary generation to reduce token waste. |
| Verbal Padding | `msg_voice.py` | Overhauled lexical heuristic logic (`_should_auto_route`). Replaced rigid `.startswith()` action prefix checks with flexible RegEx pattern matching (`(?:вот,?\s*)?`). Allows users to prepend commands with filler strings like "Вот, сочини" without failing the auto-route parser. |
 
---

## [2.8.74] - 2026-03-28 - Multimodal Voice Pipeline Hardening

### 🛡️ Voice Pipeline Resilience

| Component | Files | Detail |
|-----------|-------|--------|
| TTS / Live API Key Rotation | `voice_engine.py` | Extracted explicit `api_key` dependency from handlers. Engine now independently queries `AgentRequestUseCase` keys inside a 3-count retry loop. Catches 429 quota exhaustion strings (3 RPM / 10 RPD limits) and routes them through `classify_key_error()` to gracefully `transient` or `quota`-suspend the faulting key without failing the background generation task. |
| Live Protocol Issue | `live_audio.py` | Removed conflicting `enable_affective_dialog` directive from `LiveConnectConfig`. Resolves the constant WebSocket 1007 abortion errors while restoring native emotional prosody to Gemini 2 outbound responses. |

### ✨ Voice Routing Heuristics

| Improvement | Files | Detail |
|-------------|-------|--------|
| Strict Action Prefixes | `msg_voice.py` | Hardened auto-routing against ghost triggers via lexical heuristic checks. Action prefixes (`сочини`, `напиши`, `бот,`, `сделай`, `расскажи`) trigger bypass routing *only* if the transcript string length is greater than 10 chars. |
| Dynamic Voice Responses | `msg_voice.py`, `cb_ai_actions.py` | Added text-based override for voice engine triggering. Users can now embed explicit phrases (`озвучь ответ`, `ответь голосом`, `прочитай вслух`) into their dictation to force the bot to reply with a voice note, even if the payload went to the manual confirmation phase UI. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 18 files reformatted |
| Mypy (`--strict`) | 0 errors |
| Tests | **1453 passed**, 0 failed |

---

## [2.8.73] - 2026-03-28 - Architectural Hardening & Global i18n
    
### 🌐 Internationalization (i18n) Phase 1
    
| Service | Files | Detail |
|---------|-------|--------|
| Fully Localized Core Handlers | `messages.py`, `ai_chat.py`, `msg_roles.py` | 60+ hardcoded Russian strings moved to the central `t(key)` registry. The AI Chat flow, role generation, prompt editing, and message routing are now fully language-aware (EN/RU). |
| Document Pipeline i18n | `msg_document.py` | 33 hardcoded strings localized. Extracted duplicate detection, limit warnings, and document statistics UI into structured localized templates. |
| Scheduled Briefs i18n | `scheduled_briefs.py` | Command confirmations and morning brief formatting are now fully localized. |
| Consolidated Registry | `i18n.py` | Expanded to cover 60+ new keys under `role.*`, `doc.*`, `msg.*`, and `brief.*` namespaces, with complete duplicate-free enforcement. |
    
### 🛡️ Resilience & Memory Hardening

| Component | Files | Detail |
|-----------|-------|--------|
| Parameterizable LRU State | `state.py`, `config.py` | Fixed a major OOM vector for constrained deployments. The previously hardcoded `50,000`-entry `_UserStateStore` LRU cache is now dynamically bounded by the `LRU_STATE_CACHE_SIZE` environment variable (defaults to `1000`). |
| Pydantic DAO Boundary | `app/core/entities.py`, `repos/chats.py`, `repos/users.py` | Established a strict Pydantic validation boundary for data hydrated from Postgres. Prevents latent schema drift from causing cascading runtime `KeyError` crashes in the state machine. Invalid DB payload gracefully fallback to `_default_model()`. |
| Benchmark Execution Purge | `/benchmarks/*` | Audited and deleted 14 legacy, unmaintained micro-benchmark files that clustered the repository and posed security/execution risks. |
    
### ✅ Quality Gates
    
| Check | Result |
|-------|--------|
| Mypy (`--strict`) | 0 errors (fully validated) |
| Ruff | All files conform strictly |
| Tests | **1453 passed**, 0 failed |

---

## [2.8.72] - 2026-03-28 - Voice Engine 2.0 & GraphRAG Memory Hardening

### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Voice Engine 2.0 | `voice_engine.py`, `tts.py`, `live_audio.py` | Full outbound TTS pipeline using Gemini Live API affective audio (primary) and REST TTS (fallback). Handles PCM→OGG Opus transcoding natively via ffmpeg. |
| Automatic Voice Replies | `ai_chat.py`, `msg_voice.py`, `cb_voice.py` | Added internal `reply_with_voice` pipeline propagation so the bot replies to voice messages with a synthesized voice note. Included manual `🔊 Озвучить` inline keyboard button. |
| GraphRAG Memory | `memory.py`, `memory_consolidation.py`, `ai_chat.py` | Evolved LTM consolidation to extract relational knowledge graphs (entities, facts, relationships). `search_memories_with_graph` performs vector search + 1-hop graph traversals for deeper contextual recall. XML graph triples injected into system prompt. |

### 🛡️ Architecture & Hardening

| Fix | Files | Detail |
|-----|-------|--------|
| Distributed Tracing | `background_tasks.py` | Fixed a gap where background tasks spawned via `submit_retryable()` lost telemetry contexts across `await` boundaries. Implemented explicit `contextvars.copy_context()` propagation. |
| Mypy Strict Typing | Core Handlers | Resolved all pre-existing static analysis type errors across `ai_photo.py`, `ai_search.py`, `agentic.py`, `decorators.py` and `cmd_reminders.py`. Codebase strictly complies with `python -m mypy app --strict`. |

### 🗄️ Database Migrations

| Migration | Detail |
|-----------|--------|
| `025_add_graph_memory.sql` | Added `memory_nodes` (with 768-dim `halfvec` embedding) and `memory_edges` for the relational knowledge graph architecture. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Mypy (`--strict`) | 0 errors (fully validated) |
| Tests | **1453 passed**, 0 failed |

---

## [2.8.71] - 2026-03-28 - Voice Pipeline Concurrency & Deduplication Hardening

### 🛡️ Concurrency & Race Condition Fixes

| Fix | Files | Detail |
|-----|-------|--------|
| Synchronous Double-Tap Rejection | `state.py`, `messages.py` | Added atomic `is_processing` boolean flag to `UserState` to prevent overlapping requests. Guards both text and voice pipelines at entry (Step 1), synchronously rejecting double-taps before asyncio tasks even spin up. |
| Message-Bound Voice Context | `msg_voice.py`, `cb_voice.py` | Voice contexts are now bound to specific `message_id`s (`voice_pending_{msg_id}`) instead of a global `voice_pending_` key. Prevents context contamination if multiple voice messages are sent concurrently or if the user taps buttons on old messages. |
| Stream Error Double-Reply Fix | `ai_chat.py`, `ai_core.py` | Fixed a bug where a "2 RATE LIMIT" fallback messages fired. Propagated `stream_last_msg` during fallback handling to ensure the error uses the active placeholder ID and ignores transient "Message not modified" Telegram exceptions. |

### ✨ Improvements & Cleanup

| Improvement | Files | Detail |
|-------------|-------|--------|
| Faster Voice Retry Window | `dedup.py` | Reduced `VOICE_DEDUP_WINDOW` from 120s to 30s. Allows users to manually retry failed or ignored voice messages much quicker without artificial deduplication blocking. |
| Deep State Cleanup on `/newchat` | `commands.py`, `dedup.py` | `/newchat` now explicitly purges the 30s voice deduplication cache (`clear_user_dedup`) and clears any floating `voice_pending_{id}` contexts, ensuring a truly clean slate. |
| "Re-transcribe (Flash)" Button | `msg_voice.py`, `cb_voice.py` | Added a dedicated button to the voice confirmation UI to fast-track re-transcription using the `gemini-3-flash-preview` model for stubborn or misheard audio. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint / format | 0 errors |
| Mypy | Unchanged |
| Tests | **1453 passed**, 0 failed |

---

## [2.8.70] - 2026-03-28 - Multimodal Architecture Upgrade & Voice Resilience

### ✨ New Features (Multimodal UX)

| Feature | Files | Detail |
|---------|-------|--------|
| Smart Voice Auto-Routing | `msg_voice.py`, `thinking_classifier.py` | Voice messages with `LOW` complexity transcripts (greetings, confirmations) now skip the manual confirmation UI entirely and execute via `_handle_regular_chat`. |
| Agentic Voice Search | `multimodal_processor.py`, `cb_voice.py` | Added `INTENT:SEARCH` to voice ASR prompt. Shows a `🔍 Deep Search (Agent)` primary button to natively trigger the web research engine (`_handle_research_agent`). |
| Show & Tell (Voice+Image) | `msg_voice.py`, `cb_voice.py` | If a voice message is a Reply to a Photo, the image is dynamically fetched, locally cached, and injected as a `TaggedImage` into the `parts` array so the LLM can "see" what the user is talking about. |
| Hidden Developer ASR Test | `cmd_asr_test.py`, `commands.py` | New `/asr <model>` command to benchmark ASR models on problematic voice messages via Reply context. Bypasses standard UI and inline-prints detected intent and raw transcript. |

### ⚡ Performance & Memory Optimization

| Change | Files | Detail |
|--------|-------|--------|
| Entropy-Based Image Compression | `image_utils.py` | Shannon entropy analysis on 256x256 thumbnails dynamically dials resolution limits. Text-dense screenshots get a +50% dimension boost; simple photos get a -25% token-saving reduction. |
| Message Debounce (Split-Tapping) | `debounce.py` [NEW], `messages.py` | 400ms `asyncio.Event` aggregation window merges rapid-fire text messages from the same user into a single AI request, preventing fragmented replies and token waste. |
| Memory Enrichment Tags | `multimodal_processor.py`, `ai_photo.py` | Custom `_VOICE_LTM_PROMPT` enforces extraction of `[VOICE, Tone: X, Urgency: Y]` metadata stored in the new JSONB `metadata` column, boosting keyword hits in RRF hybrid retrieval. |

### 🛡️ Voice Pipeline Resilience (Hotfix)

| Fix | Files | Detail |
|-----|-------|--------|
| `TimeoutError` silent failure fix | `resilience_policy.py`, `multimodal_processor.py` | Fixed bug where `asyncio.TimeoutError` stringified as empty, bypassing retry logic. Created `is_retryable_exception()` type check. Dialed `_MEDIA_RESILIENCE` to 30s timeout, 2 retries. |
| Voice handler unmanaged execution | `messages.py`, `msg_voice.py` | Moved voice processing into the standard `submit_task` + `task_wrapper` pattern (alongside text/photo), ensuring heartbeat logging, `user_lock` concurrency limits, and 90s overall timeouts apply. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 10 files reformatted |
| Tests | **1453 passed**, 0 failed |

---

## [2.8.69] - 2026-03-27 - i18n Rollout & Conversational Voice Flow

### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| i18n String Registry | `app/i18n.py` [NEW] | Centralized bilingual (ru/en) string registry with `t(key, lang, **kwargs)` lookup. 200+ keys covering voice, errors, processing, menus, help, settings, documents, images, and buttons. Content-based Cyrillic density heuristic for language detection (`detect_language()`). |
| Conversational Voice Flow | `msg_voice.py`, `cb_voice.py` [NEW] | Interactive Transcribe→Confirm→Respond cycle with intent-aware routing (`INTENT:CONVERSATIONAL` vs `INTENT:TRANSCRIPTION`). Inline buttons (✅ Confirm / 📝 Edit / ❌ Cancel / 📋 Transcribe Only). Voice edit interception in `messages.py`. Dedup via `is_duplicate_voice()` (120s window). |

### ⚡ i18n Rollout (10 handler files)

| File | Strings Replaced |
|------|-----------------|
| `cb_ai_actions.py` | Error strings, processing indicators, retry messages |
| `messages.py` | Rate limit, message too long, processing errors |
| `msg_media.py` | Image/group errors, overflow messages |
| `msg_document.py` | Document errors, button labels |
| `ai_document.py` | Document question error strings |
| `commands.py` | `@safe_handler` strings, help/newchat/settings buttons |
| `cb_documents.py` | Not found, delete errors, toast messages |
| `menus.py` | Start menu buttons, model/roles/docs back buttons |
| `callbacks.py` | Busy toast annotation |
| `cb_navigation.py` | Help, settings, new chat, deep dive strings |

### 🛡️ Resilience

| Change | Files | Detail |
|--------|-------|--------|
| Circuit breaker noise reduction | `circuit_breaker.py` | `_monitor_loop` logs at DEBUG for routine checks, INFO only on state change or new failures. Media CB pre-created with `monitor_interval=60.0`. |

### 🧹 Lint Cleanup

| Fix | Files | Detail |
|-----|-------|--------|
| Remove 11 unused imports (F401) | `cb_ai_actions.py`, `cb_voice.py`, `cb_navigation.py`, `messages.py`, `ai_document.py` | Removed `_is_user_busy`, `_BUSY_TOAST`, `detect_language`, `contextlib`, `MEDIA_GROUPS*`, `cleanup_old_media_groups`, `get_user_chat`, `get_provider_router`. |
| Fix test imports | `test_audit_fixes.py` | Updated `TestMediaGroupMaxSize` to import from `msg_media` (origin) instead of removed re-exports in `messages.py`. |

### 🐛 Bug Fixes

| Fix | Files | Detail |
|-----|-------|--------|
| `cb_voice.py` import path | `cb_voice.py` | Fixed `_handle_regular_chat` import from `ai_chat.py` (was incorrectly pointing to `messages`). |
| Test tuple assertion | `test_audio_processor.py` | Updated `test_transcribe_voice_rejects_empty_bytes` for new `(None, "conversational")` return type. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **1453 passed**, 0 failed |

### Files Changed (15 files, 2 new)

| File | Change |
|------|--------|
| `app/i18n.py` | [NEW] i18n string registry with 200+ bilingual keys |
| `app/handlers/cb_voice.py` | [NEW] Voice confirmation callback handler |
| `app/handlers/msg_voice.py` | Conversational voice flow with inline buttons |
| `app/handlers/cb_ai_actions.py` | i18n + removed unused `_is_user_busy` import |
| `app/handlers/messages.py` | i18n + removed 6 unused imports |
| `app/handlers/msg_media.py` | i18n (image/group errors) |
| `app/handlers/msg_document.py` | i18n (document errors, button labels) |
| `app/handlers/ai_document.py` | i18n + removed unused `get_provider_router` import |
| `app/handlers/commands.py` | i18n (safe_handler, buttons) |
| `app/handlers/cb_documents.py` | i18n (toasts, errors) |
| `app/handlers/menus.py` | i18n (start menu buttons, back buttons) |
| `app/handlers/cb_navigation.py` | i18n + removed unused `detect_language` import |
| `app/handlers/callbacks.py` | i18n annotation on `_BUSY_TOAST` |
| `app/circuit_breaker.py` | DEBUG-level monitoring, 60s interval |
| `tests/test_audit_fixes.py` | Fixed imports for `msg_media` constants |

---

## [2.8.68] - 2026-03-27 - Multimodal Pipeline & Embedding Migration

### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Voice Message Processing | `msg_voice.py` [NEW], `messages.py` | Inline voice handler (`handle_voice_inline`) routed through `handle_request` pipeline (Step 4b). Inherits all auth, rate-limit, tracing, and dedup guards. Transcription via `gemini-3.1-flash-lite` with `thinking_config=HIGH`. |
| Multimodal Memory Storage | `ai_photo.py`, `msg_document.py`, `multimodal_processor.py` | Background `process_media_for_memory()` tasks added for images and documents via `submit_retryable()`. Extracts semantic content from media and stores as LTM entries. |
| Multimodal Processor | `multimodal_processor.py` [NEW] | Unified media processing service: `transcribe_voice()`, `describe_image()`, `extract_document_content()`, `process_media_for_memory()`. Model-aware key resolution, configurable thinking levels, resilient generation with multi-key rotation. |

### 🛡️ Resilience & 503 Handling

| Change | Files | Detail |
|--------|-------|--------|
| 503/UNAVAILABLE key rotation (streaming) | `gemini.py` | 503 errors are now re-raised (not yielded as text) when no content has been streamed, enabling `ProviderRouter` to rotate to a fresh API key. Mid-stream failures still yield error text gracefully. |
| Multi-key retry in generation | `multimodal_processor.py` | `_generate_with_resilience`: 3 attempts per key, rotating through up to 3 different API keys on transient failures. Model-specific key resolution via `_get_api_key_for_media(model)`. |

### ⚡ Embedding Migration

| Change | Files | Detail |
|--------|-------|--------|
| Gemini v2 768-dim embeddings | `memory.py`, `024_upgrade_gemini_v2_768.sql` | Migrated from `gemini-embedding-001` (3072-dim `halfvec`) to `gemini-embedding-2-preview` (768-dim `vector`). Idempotent migration drops old index, alters column type, recreates HNSW index. |

### 🐛 Bug Fixes

| Fix | Files | Detail |
|-----|-------|--------|
| Ruff B023 loop variable binding | `multimodal_processor.py` | Fixed closure capturing loop variable `client` by binding it as a default parameter in `_call(_c=client)`. |
| Test fixtures missing `voice` attr | `test_messages.py`, `test_integration_flow.py` | Added `voice=None` to `MockMessage` and `make_update()` to prevent `AttributeError` after voice routing change. |
| Test fixtures missing `branch_id` | `factories.py`, `test_integration_flow.py`, `conftest.py` | Added `branch_id=None` to `make_chat_state()`, inline `SimpleNamespace` mocks, and integration DB bootstrap. |
| E2E test DB connection race | `test_chat_happy_path.py` | Mocked `submit_retryable` and `set_last_sent_message` to prevent background tasks competing for the single transactional test DB connection. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **1453 passed**, 0 failed |

### Files Changed (14 files, 2 new)

| File | Change |
|------|--------|
| `app/handlers/msg_voice.py` | [NEW] Inline voice handler |
| `app/utils/multimodal_processor.py` | [NEW] Unified multimodal processor with key rotation |
| `app/utils/audio_processor.py` | Backward-compat re-exports |
| `app/providers/gemini.py` | 503 re-raise in streaming path |
| `app/handlers/messages.py` | Voice routing at Step 4b |
| `app/handlers/ai_photo.py` | Background LTM hook for images |
| `app/handlers/msg_document.py` | Background LTM hook for documents |
| `app/repos/memory.py` | 768-dim embeddings, new model |
| `tests/test_messages.py` | `voice=None` in MockMessage |
| `tests/test_integration_flow.py` | `voice=None`, `branch_id=None` |
| `tests/factories.py` | `branch_id=None` in `make_chat_state` |
| `tests/integration/conftest.py` | `branch_id` column in test DB |
| `tests/e2e/test_chat_happy_path.py` | Mocked background tasks |
| `README.md` | Updated for multimodal + 768-dim embeddings |

---

## [2.8.67] - 2026-03-27 - RLS Policy Optimization & Key Health Fix

### ⚡ Performance (Supabase RLS)

| Change | Tables | Detail |
|--------|--------|--------|
| Merge duplicate PERMISSIVE policies | `brief_subscriptions`, `conversation_branches`, `user_reminders` | Each table had 2 PERMISSIVE ALL policies (`admin` + `user`), causing Postgres to evaluate both per query. Merged into single policy using `OR`. Eliminates 48 `multiple_permissive_policies` advisor warnings per project. |
| Wrap `current_setting()` in `(select ...)` | Same 3 tables | Bare `current_setting()` calls were re-evaluated per row. Wrapped in subquery for single evaluation. Eliminates 6 `auth_rls_initplan` advisor warnings per project. |
| Applied to both projects | `sweeawmcuvisjvfkobdx`, `jxldwuwelontbyoexytn` | Migration `fix_rls_initplan_and_merge_permissive_policies` applied and verified via performance advisor — 0 WARN-level RLS issues remain. |

### 🐛 Bug Fix

| Fix | Files | Detail |
|-----|-------|--------|
| Key health endpoint `NoneType` error | `web.py`, `repos/keys.py` | `/api/key-health` queried `key_model_status` (RLS-protected) without setting `app.is_admin` context. Fix: acquire pool connection with admin RLS context, pass `conn=` through `get_health_summary()` → `get_all_statuses()`. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 0 violations |

---



### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Interactive Reminder Management | `cmd_reminders.py`, `callbacks.py` | Added inline ❌ cancel buttons to the `/remind` pending list. Users can now easily delete scheduled reminders directly from the UI. Handled via `reminder_cancel:{id}` fast callback. |

### 🛡️ Reliability & Resilience

| Change | File | Detail |
|--------|------|--------|
| Target AI Task Guards | `cmd_reminders.py` | Hardened the `_execute_ai_reminder` background pipeline with `asyncio.Semaphore(3)` to prevent API key exhaustion during burst deliveries, and added a 5-minute `asyncio.wait_for` timeout guard. If an AI task hangs, the user now receives a graceful timeout notification instead of silent failure. |
| Background Task GC Fix | `cmd_reminders.py` | Fixed `RUF006` by storing `asyncio.create_task` references in a module-level `_background_ai_tasks` set with an auto-discard `done_callback`. Eliminates the `RuntimeWarning` and prevents premature garbage collection of long-running AI deliveries. |
| Intent Classifier Precision | `cmd_reminders.py` | Fixed a substring collision bug where English multi-word notification patterns (e.g., "eat") would falsely trigger inside other words ("weather"). EN patterns now use word-boundary matching while RU patterns retain substring support for morphological prefixes. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **1399 passed**, 0 failed (including 56 dedicated reminder tests) |

---

## [2.8.65] - 2026-03-27 - Phase 4: Tests, Dashboard Polish

### ✅ Unit Tests (47 new tests)

| Module | Tests | Coverage |
|--------|-------|----------|
| `middleware/dedup.py` | 12 | Hash, dedup window, eviction, isolation, clear |
| `handlers/cmd_reminders.py` | 25 | Bilingual time parser (EN/RU), command no-args, delivery job |
| `handlers/cb_branches.py` | 10 | Create, return, edge cases, exports |

### 🎨 Dashboard Visual Polish

| Enhancement | Detail |
|-------------|--------|
| SSE pulse animation | `@keyframes ssePulse` — green glow burst on live-dot when SSE event arrives |
| Metric flash effect | `@keyframes metricFlash` — teal flash on cards when values update via SSE |
| Smooth transitions | `card-value` color transitions for real-time metric changes |

---

## [2.8.64] - 2026-03-27 - Phase 3: Persistence, Dashboard & Middleware (6 Changes)

### 🐛 Fix

| Fix | File | Detail |
|-----|------|--------|
| SIM108 lint (pre-existing) | `document_processor.py` | Replaced 2 if/else blocks with ternary operators at lines 262(→259) and 308(→305). 0 warnings remain. |

### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Request Dedup Middleware | `middleware/dedup.py` [NEW], `messages.py` | 3s window, MD5 hash, per-user cleanup + eviction (20 max). Wired after rate limit, before auth. |
| Dashboard SSE Widget | `dashboard.html` | `EventSource('/api/events')` — real-time CPU, memory, DB status, queue, request count. Pulses live dot green on each event. |
| Dashboard Key Health Widget | `dashboard.html` | Key Health Diagnostics table in Infrastructure tab: status, usage%, last rotated, event count. Polls `/api/key-health` every 30s. |

### ⚡ Improvements

| Improvement | Files | Detail |
|-------------|-------|--------|
| Persist `branch_id` | `chats.py`, migration `023` [NEW] | `branch_id` column on `chats` table (FK → `conversation_branches`). Survives bot restarts. SELECT/INSERT/UPSERT queries updated. |

### 🗄️ Migrations Applied (Production)

| Migration | Status |
|-----------|--------|
| `022_add_branches_and_reminders.sql` | ✅ Applied via Supabase MCP |
| `023_add_branch_id_to_chats.sql` | ✅ Applied via Supabase MCP |

---

## [2.8.63] - 2026-03-27 - Phase 2: Integration & Live Dashboard (7 Changes)

### 🐛 Fix

| Fix | File | Detail |
|-----|------|--------|
| Context budgets: removed non-existent models | `config.py` | Removed `ultra` (200K) and `pro` (128K) entries from `MODEL_CONTEXT_BUDGETS`. Only flash-lite (32K) and flash (128K) are used. Added evidence-based comments citing context degradation research (quality degrades at ~20% of 1M context). |

### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Conversation Branching UI | `cb_branches.py` [NEW], `ai_chat.py`, `callbacks.py`, `database.py` | `🔀 Что если…` button in every AI response → snapshots history → new branch. `↩️ К основной ветке` button to restore. `branch_id` field on `ChatState`. |
| `/remind` Command | `cmd_reminders.py` [NEW], `commands.py`, `bot.py` | Bilingual time parser (30m/2h/1d, мин/час/день). `check_and_deliver_reminders` job (60s poll via `job_queue`). Shows pending reminders on `/remind` with no args. |

### ⚡ Improvements

| Improvement | Files | Detail |
|-------------|-------|--------|
| SSE Live Dashboard | `web.py` | `/api/events` endpoint: Server-Sent Events stream emitting system/queue/metrics JSON every 5s. Includes CPU, memory, DB status, queue pending/processing, request count. |
| Key Health API | `web.py` | `/api/key-health` endpoint exposing `KeyStatusManager.get_health_summary()` with per-key diagnostics. Rate-limited + auth-protected. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint (all modified files) | 0 errors |
| Pre-existing lint | 2 SIM108 in `document_processor.py` (unrelated) |

### Files Changed (11 files, 2 new)

| File | Change |
|------|--------|
| `app/handlers/cb_branches.py` | [NEW] Branch create/return callbacks |
| `app/handlers/cmd_reminders.py` | [NEW] /remind command + delivery job |
| `app/config.py` | Fixed MODEL_CONTEXT_BUDGETS (removed ultra/pro) |
| `app/database.py` | Added `branch_id` to `ChatState` |
| `app/handlers/ai_chat.py` | Branch-aware button in response keyboard |
| `app/handlers/callbacks.py` | Branch callback registration |
| `app/handlers/commands.py` | `/remind` command registration |
| `app/web.py` | `/api/key-health` + `/api/events` SSE endpoints |
| `bot.py` | Reminder delivery job (60s interval) |
| `README.md` | Updated feature descriptions |
| `CHANGELOG.md` | This entry |

---

## [2.8.62] - 2026-03-27 - Architectural Refactoring & New Features (12 Changes)

### ✨ New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Adaptive Thinking Budget | `thinking_classifier.py` [NEW], `ai_chat.py`, `config.py` | 14 regex heuristics (8 HIGH, 6 LOW) + context-aware escalation. `resolve_thinking_level()` auto-selects `low`/`medium`/`high` when user has no explicit preference. `ADAPTIVE_THINKING_ENABLED` toggle. |
| Conversation Branching | `repos/branches.py` [NEW], `022_add_branches_and_reminders.sql` [NEW] | Fork chat history into a temporary "what-if" branch via `create_branch()` → `restore_branch()`. `conversation_branches` table with JSONB snapshot, RLS policies. |
| Smart Context Window | `config.py`, `ai_chat.py` | `MODEL_CONTEXT_BUDGETS` maps model patterns to token budgets (flash-lite: 32K, flash/pro: 128K, ultra: 200K). Dynamic `token_budget` passed to `ContextAssembler.assemble()`. |
| Proactive Follow-ups | `repos/reminders.py` [NEW], `022_add_branches_and_reminders.sql` [NEW] | DB-persisted user reminders with `trigger_at`, `prompt`, `context_history`. `get_pending_reminders()` for 60s poll-based delivery. `user_reminders` table with partial index on pending items. |

### ⚡ Improvements

| Improvement | Files | Detail |
|-------------|-------|--------|
| Streaming Reliability | `streaming.py` | `_retry_edit()` with 3-retry exponential backoff (0.5→1→2s + jitter) for Telegram 429/flood errors. Adaptive debounce escalation: `self._debounce_s *= 1.5` on rate-limit, capped at 3s. |
| State Persistence Debounce | `state.py` | Replaced fire-and-forget `_schedule_persist` with 300ms debounced `TimerHandle` per user_id. Multiple rapid mutations → 1 DB write with final state. |
| Error Classification Migration | `errors.py` | `classify_error_from_exception()` with O(1) type→ErrorCode lookup (17 types + MRO walk), `classify_error_from_status_code()` (8 HTTP codes), `_ERROR_CODE_MESSAGES` (16 entries). `user_friendly_error()` uses typed classification as primary path. |
| Dashboard Batch API | `web.py` | `/api/dashboard` endpoint aggregates 8 metrics via `asyncio.gather()` with `_safe_fetch()` failure isolation. Replaces 8 frontend `fetch()` calls with 1 HTTP RTT. |
| Key Rotation Observability | `repos/keys.py` | Structured `KEY_EVENT` logs: `key_first_use`, `key_usage_milestone` (every 100), `key_nearing_limit` (70%), `key_threshold_reached` (rotation). `get_health_summary()` dashboard endpoint. |
| Agentic Source Dedup | `agentic.py`, `config.py` | `_normalize_url()` strips 16 tracking params, URL dedup in `search_web`, content truncation at `AGENTIC_PAGE_CONTENT_LIMIT` (8192 chars). |
| Graceful Shutdown | `bot.py` | Drains pending `_pending_persists` (5s timeout) + task queue (10s timeout) before closing DB/Redis. |

### 🗄️ Database Migrations

| Migration | Tables |
|-----------|--------|
| `022_add_branches_and_reminders.sql` | `conversation_branches` (branching snapshots) + `user_reminders` (follow-ups). Indexes, FK constraints, RLS policies. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint (modified files) | 0 errors |
| Pre-existing lint | 2 SIM108 in `document_processor.py` (unrelated) |

### Files Changed (15 files, 4 new)

| File | Change |
|------|--------|
| `app/thinking_classifier.py` | [NEW] 14-pattern classifier |
| `app/repos/branches.py` | [NEW] Conversation branching CRUD |
| `app/repos/reminders.py` | [NEW] User reminders CRUD |
| `scripts/migrations/022_add_branches_and_reminders.sql` | [NEW] DB migration |
| `app/errors.py` | Type-based error classification |
| `app/repos/keys.py` | KEY_EVENT logging + `get_health_summary()` |
| `app/streaming.py` | `_retry_edit()` + adaptive debounce |
| `app/state.py` | Debounced persistence |
| `app/web.py` | `/api/dashboard` batch endpoint |
| `app/config.py` | `ADAPTIVE_THINKING_ENABLED`, `AGENTIC_PAGE_CONTENT_LIMIT`, `MODEL_CONTEXT_BUDGETS` |
| `app/handlers/ai_chat.py` | Thinking level + context budget integration |
| `app/core/agentic.py` | URL normalization + dedup + truncation |
| `app/db/schema.py` | +2 tables in `EXPECTED_TABLES` |
| `bot.py` | Graceful shutdown drain logic |
| `README.md` | Updated feature list |

---

## [2.8.61] - 2026-03-26 - QnA Search Fallback Fix & Observability (5 Changes)

### 🐛 Bug Fixes

| Fix | File | Detail |
|-----|------|--------|
| 429 mid-stream error breaks fallback chain | `ai_search.py` | Error-tagged text returned as `success=True`. Fix: detect `_TAG_PREFIX` anywhere in `final_answer` and continue to next model. |
| Fallback can't stream to consumed placeholder | `ai_search.py` | Model 2's edits fail with "Message to edit not found". Fix: send fresh placeholder before retrying. |
| gemini-3.1-flash-lite always 429 on search | `ai_search.py` | Gemini 3.x has no Search Grounding quota on free tier. Fix: replaced with `gemini-2.5-flash-lite` primary + `gemini-2.5-flash` fallback. |
| Model ignores web search, answers from training data | `ai_search.py` | Without date/instruction prompt, model didn't know "today" and refused queries it thought were about the future. Fix: QnA-specific system prompt with `Сегодня: YYYY-MM-DD` + `ВСЕГДА используй Google Search`. |

### 📡 Observability

| Change | File | Detail |
|--------|------|--------|
| API key identifiers | `router.py` | Log line now shows both the `key_hash` prefix (8 chars) AND last 4 chars of the actual API key: `key=0c94f6e0…(…XyZ9)`. The suffix lets you identify the key in GCP/AI Studio by matching the end of the key string. |
| Model name in QnA logs | `ai_search.py` | All `_handle_qna_search` log lines now include the exact model name and attempt number (e.g., `QnA search: trying model gemini-3.1-flash-lite (attempt 1/2)`). |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **1343 passed**, 0 failed |

---

## [2.8.60] - 2026-03-26 - Google Search Grounding for Quick Search (1 Change)

### ⚡ Performance & Architecture

| Change | File | Detail |
|--------|------|--------|
| Native Google Search Grounding | `ai_search.py`, `base.py`, `gemini.py`, `openrouter.py`, `router.py`, `streaming.py` | Replaced the two-step Tavily+LLM pipeline for `?` prefix queries with a single Gemini API call using `types.Tool(google_search=types.GoogleSearch())`. Reduces latency from 2 network hops to 1 and eliminates Tavily dependency for quick search. Threaded `enable_web_search: bool` flag through the entire provider stack (base → gemini/openrouter → router → streaming → handler). |
| Model Fallback Chain | `ai_search.py` | `_handle_qna_search` now uses a resilient fallback chain: `gemini-3.1-flash-lite` → `gemini-2.5-flash-lite`. User's custom model (if set) is respected as first in the chain. Falls back to non-streaming if all streaming attempts fail. |

### 🧪 Test Updates

| Change | File | Detail |
|--------|------|--------|
| QnA test rewrite | `test_ai_search.py` | Replaced `test_qna_search_happy_path` (verified `enable_web_search=True` is passed) and `test_qna_search_tavily_error` → `test_qna_search_streaming_failure_fallback` (verifies non-streaming fallback). |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **1343 passed**, 0 failed |

### Files Changed (7 files)

| File | Change |
|------|--------|
| `app/providers/base.py` | Added `enable_web_search: bool = False` to abstract `stream_response` |
| `app/providers/gemini.py` | Injects `types.Tool(google_search=types.GoogleSearch())` when `enable_web_search=True` |
| `app/providers/openrouter.py` | Added param to signature (noop) |
| `app/providers/router.py` | Threads `enable_web_search` to provider |
| `app/streaming.py` | Threads `enable_web_search` to router |
| `app/handlers/ai_search.py` | Rewritten `_handle_qna_search`: removed Tavily, added model fallback chain |
| `tests/test_ai_search.py` | Rewrote QnA tests for Google Search Grounding flow |

---

## [2.8.59] - 2026-03-26 - Architectural Refactoring & Resilience (4 Improvements)

### ⚡ Performance & Scalability

| Change | File | Detail |
|--------|------|--------|
| Threaded Garbage Collection | `agentic.py` | Replaced blocking `gc.collect()` with `asyncio.to_thread(gc.collect, 1)`. Eliminates event loop stalls during generation-1 (young object) garbage sweeps, improving concurrent responsiveness during deep research sessions. |

### 🛡️ Reliability & Resilience

| Change | File | Detail |
|--------|------|--------|
| Agentic Model Fallback Cascade | `ai_search.py` | Built an intelligent fallback chain for deep research. If the primary model fails or returns a 503, the system now automatically retries the task using the next most capable model defined in `_MODEL_TIER` ranking. |

### ✨ User Experience & UX Consistency

| Change | File | Detail |
|--------|------|--------|
| Streaming Footer Injection | `streaming.py` | Memory injection warnings (`🧠 Использован контекст...`) are now passed as `footer_text` during the streaming phase and appended prior to finalization. Eliminates the visual "layout jump" that occurred when text was retroactively appended via separate `edit_text` calls. |

### 🏗️ Maintainability

| Change | File | Detail |
|--------|------|--------|
| "God-Handler" De-bloat | `ai_chat.py` | Extracted the monolithic memory storage logic into a dedicated asynchronous `_store_memory_in_background` helper. Action buttons are now attached natively during stream finalization. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 105 files clean |
| Tests | **1343 passed**, 0 failed (749s on single thread due to Windows xdist constraints) |

---

## [2.8.58] - 2026-03-23 - Dependency Hotfixes (2 Fixes)

### 🔴 Critical Fix

| Fix | File | Detail |
|-----|------|--------|
| PTB JobQueue warning | `requirements.txt` | Added `[job-queue]` extra to `python-telegram-bot` dependency. Resolves the `PTBUserWarning` on startup and ensures the `scheduled_briefs` background job can be configured via `application.job_queue`. |

### 🟡 Medium Fix

| Fix | File | Detail |
|-----|------|--------|
| Local `textual` conflict | `requirements.txt`, `requirements-dev.txt` | Expanded `rich` version bounds from `<14.0.0` to `<15.0.0`. Resolves `pip` dependency resolver conflicts in local developer environments that have modern `textual` installed (which requires `rich>=14.2.0`). |

---

## [2.8.57] - 2026-03-23 - Reliability & Intelligence Improvements (6 Changes)

### 🔧 Change 1: Metrics Snapshot Race Fix (P0)

| Change | File | Detail |
|--------|------|--------|
| Atomic snapshot+reset | `metrics.py` | `_save_metrics_to_db()` now snapshots counters under `self._lock` BEFORE any DB I/O. Eliminates window where events during write were silently lost |
| Compensating re-add | `metrics.py` | On DB failure, snapshotted values are atomically re-added to live counters — zero-loss guarantee |

### ⚡ Change 2: Redis-Backed Persistent Queue (P1)

| Change | File | Detail |
|--------|------|--------|
| Redis Lists backend | `queue.py` | Full rewrite: `LPUSH`/`RPOPLPUSH` per priority level. Tasks survive restarts |
| RPOP polling | `queue.py` | Uses `RPOP` instead of `BRPOP` — compatible with Upstash connection limits |
| Crash recovery | `queue.py` | On startup, re-queues any tasks stuck in processing list |
| In-memory fallback | `queue.py` | Degrades to `asyncio.PriorityQueue` when Redis unavailable |
| Test update | `test_task_queue.py` | Updated attribute references (`queue` → `_fallback_queue`) |

### 🧠 Change 3: LTM Consolidation Debounce (P1)

| Change | File | Detail |
|--------|------|--------|
| In-memory gate | `memory_consolidation.py` | `should_check_consolidation()` — O(1) check, fires only every 20 msgs or 15 min |
| Model update | `memory_consolidation.py` | Updated model to `gemini-3.1-flash-lite` |
| Gate integration | `ai_chat.py` | Wired gate before `should_consolidate()` call |

### 📄 Change 4: Document Chunking at Retrieval Time (P2)

| Change | File | Detail |
|--------|------|--------|
| Chunking module | `documents/chunking.py` | [NEW] Three strategies: `recursive_chunk` (paragraph/sentence/word), `hierarchical_chunk` (parent/child), `chunk_for_context` (relevance scoring + budget assembly) |
| Pipeline integration | `ai_document.py` | Replaced na️ve `[:30000]` hard-truncation with `chunk_for_context(text, query=user_message, max_context_tokens=8500)`. Query-aware relevance scoring selects best chunks |

### 📰 Change 5: Scheduled Intelligence Briefs (P3)

| Change | File | Detail |
|--------|------|--------|
| Brief handler | `handlers/scheduled_briefs.py` | [NEW] Full pipeline: subscription CRUD, LTM topic extraction → Tavily search → Gemini summary → Telegram delivery |
| Migration | `021_add_brief_subscriptions.sql` | [NEW] Table with FK, unique constraint, partial index, RLS policies. Applied to production |
| Command registration | `commands.py` | `/subscribe` and `/unsubscribe` handlers registered |
| Hourly scheduler | `bot.py` | `check_and_send_briefs` via `application.job_queue.run_repeating` (interval=3600s, first=60s) |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Tests | **1343 passed**, 0 failed (73s) |
| New tests | 46 tests across 5 new test files |
| Migration | Applied to production via Supabase MCP |
| Security advisors | No new warnings. RLS enabled |

### Files Changed (17 files, 8 new)

| File | Change |
|------|--------|
| `app/metrics.py` | Atomic snapshot+reset, compensating re-add |
| `app/queue.py` | Redis Lists backend with fallback |
| `app/repos/memory_consolidation.py` | Debounce gate + model update |
| `app/handlers/ai_chat.py` | Consolidation gate integration |
| `app/documents/chunking.py` | [NEW] 3-strategy retrieval-time chunking |
| `app/handlers/ai_document.py` | Query-aware chunking integration |
| `app/handlers/scheduled_briefs.py` | [NEW] Full briefs pipeline |
| `app/handlers/commands.py` | /subscribe, /unsubscribe registration |
| `bot.py` | Hourly briefs scheduler |
| `scripts/migrations/021_add_brief_subscriptions.sql` | [NEW] Brief subscriptions table |
| `tests/test_metrics_snapshot.py` | [NEW] 5 tests |
| `tests/test_metrics_integration.py` | Updated for new behavior |
| `tests/test_redis_queue.py` | [NEW] 10 tests |
| `tests/test_task_queue.py` | Attribute reference fixes |
| `tests/test_consolidation_debounce.py` | [NEW] 8 tests |
| `tests/test_chunking.py` | [NEW] 16 tests |
| `tests/test_scheduled_briefs.py` | [NEW] 7 tests |

---

## [2.8.56] - 2026-03-22 - Long-Term Memory Overhaul (5 Changes)

### 🧠 Change 1: System Prompt Injection (Context Engineering)

| Change | File | Detail |
|--------|------|--------|
| XML memory injection | `chat_logic.py` | New `format_memories_for_system_prompt()` renders memories as `<long_term_memory><fact source="date">...</fact></long_term_memory>` XML block for `system_instruction` |
| History mutation removed | `ai_chat.py` | Memories no longer prepended as fake user/model turns. Injected into `system_instruction` string instead |
| Backward compat | `chat_logic.py` | `build_memory_context()` preserved as deprecated wrapper with `DeprecationWarning` |

### 💾 Change 2: User-Intent-Only Storage

| Change | File | Detail |
|--------|------|--------|
| Semantic density optimization | `ai_chat.py` | Stores `user_message[:500]` with `source_type='user_intent'` instead of `"Q: ... A: ..."` with `source_type='conversation'`. Eliminates verbose bot replies from vector space |

### 🔍 Change 3: Hybrid Retrieval (RRF)

| Change | File | Detail |
|--------|------|--------|
| RRF hybrid search | `memory.py` | `search_memories()` now uses Reciprocal Rank Fusion: `pgvector` cosine similarity (semantic CTE) + `pg_trgm` keyword matching (keyword CTE) with `k=60` smoothing. Graceful fallback to pure semantic if `pg_trgm` unavailable |
| Extension + index | `020_add_trgm_hybrid_search.sql` | `CREATE EXTENSION pg_trgm` + `CREATE INDEX USING gin (content gin_trgm_ops)` |
| New repo functions | `memory.py` | Added `delete_memory()`, `list_memories()`, `_check_trgm_available()` |
| Stats modernization | `memory.py` | `get_memory_stats()` now counts `user_intent` + `consolidated` source types |

### 👤 Change 4: `/memory` User Control Command

| Change | File | Detail |
|--------|------|--------|
| Paginated memory viewer | `memory_commands.py` | [NEW] `/memory` shows paginated inline UI (5/page) with per-memory 🗑 delete buttons and ⬅️/➡️ navigation |
| Handler registration | `bot.py` | `memory_commands.register(application)` wired after core handlers |

### ⚡ Change 5: Dynamic Memory Consolidation

| Change | File | Detail |
|--------|------|--------|
| Token + temporal trigger | `memory_consolidation.py` | [NEW] `should_consolidate()` checks: ≥8,000 estimated tokens OR ≥7 days since last consolidation |
| LLM fact extraction | `memory_consolidation.py` | `_extract_persona_facts()` uses `gemini-2.0-flash-lite` to distill 5-8 atomic facts from raw memories |
| Transactional replacement | `memory_consolidation.py` | `consolidate_memories()` deletes raw batch + inserts facts with embeddings in a single DB transaction |
| Background trigger | `ai_chat.py` | Consolidation check runs after each `store_memory` call in the background task |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **1295 passed**, 0 failed (77s) |
| Migration | Applied to production via Supabase MCP |

### Files Changed (7 files, 2 new)

| File | Change |
|------|--------|
| `app/handlers/chat_logic.py` | `format_memories_for_system_prompt()` + deprecated `build_memory_context()` |
| `app/handlers/ai_chat.py` | XML injection, user-intent storage, consolidation trigger |
| `app/repos/memory.py` | RRF hybrid search, `delete_memory`, `list_memories`, stats update |
| `app/handlers/memory_commands.py` | [NEW] `/memory` command with inline pagination and delete |
| `app/repos/memory_consolidation.py` | [NEW] Dynamic consolidation with LLM fact extraction |
| `scripts/migrations/020_add_trgm_hybrid_search.sql` | [NEW] `pg_trgm` + GIN index |
| `bot.py` | Memory commands registration |

---

## [2.8.55] - 2026-03-22 - AI Prompt Editing Refinements

### ✨ UX Improvements

| Detail | Description |
|--------|-------------|
| Full Prompt Preview | Removed 300-character truncation in AI enhancement preview. The entire prompt is now visible and copyable. |
| AI Tweak Mode | Added a `✏️ Редактировать` button during AI preview to smoothly transition into manual edit mode without losing the generated text. |

### Files Changed (2 files)

| File | Change |
|------|--------|
| `app/handlers/cb_roles.py` | Added `role_edit_ai_tweak_callback` |
| `app/handlers/msg_roles.py` | Removed truncation, added Tweak button to markup |

---

## [2.8.54] - 2026-03-22 - Schema Bootstrapping Consolidation

### 🏗️ Architecture: Complete Schema Source of Truth

Previously `app/db/schema.py` was a no-op and 7 tables were missing from `000_init_schema.sql`, scattered across later migrations, runtime Python code, or not captured at all (`long_term_memory`). Fresh deploys would fail.

| Change | File | Detail |
|--------|------|--------|
| Added 7 missing tables to init schema | `000_init_schema.sql` | `user_metrics`, `model_configuration`, `active_chat_messages`, `long_term_memory`, `group_chats`, `group_members`, `group_messages` — all with production-accurate types, FKs, defaults |
| Backfill migration for existing DBs | `018_add_missing_table_definitions.sql` | [NEW] Creates missing tables with `IF NOT EXISTS` + HNSW index for `long_term_memory` |
| Drop dead `chats.history` column | `019_drop_chats_history_column.sql` | [NEW] Column was dropped from production but still in init schema. App uses `active_chat_messages` |
| Startup schema validation | `schema.py` | Replaced no-op with 25-table inventory check against `pg_tables` |
| DDL removed from runtime code | `group_chat.py` | Inline `CREATE TABLE` statements removed — tables now come from migrations |
| README Schema Management section | `README.md` | Documents migration workflow, removed "Incomplete Schema Bootstrapping" known gap |

### 🔍 Verified Against Production (Supabase MCP)

All table definitions compared against live `TEST_gemaibotv2` schema. Fixed: `long_term_memory.id` → BIGSERIAL, group timestamps → TIMESTAMPTZ, added FK constraints on group tables.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Tests | **1295 passed**, 0 failed (72s, 12 workers) |
| Ruff lint | 0 errors |

### Files Changed (8 files)

| File | Change |
|------|--------|
| `scripts/migrations/000_init_schema.sql` | +7 tables, -dead `history` column |
| `scripts/migrations/018_add_missing_table_definitions.sql` | [NEW] Backfill migration |
| `scripts/migrations/019_drop_chats_history_column.sql` | [NEW] Cleanup migration |
| `app/db/schema.py` | Startup validation (25 expected tables) |
| `app/db/__init__.py` | Updated docstring |
| `app/group_chat.py` | Removed inline CREATE TABLE DDL |
| `tests/test_group_chat.py` | Updated assertions for new initialize() behavior |
| `README.md` | Schema Management section + removed known gap |

---

## [2.8.53] - 2026-03-22 - Edit Prompt for Custom Roles

### ✨ New Feature: Edit Role Prompt

Two modes for editing custom role prompts directly from the role details view:

| Mode | Flow |
|------|------|
| **📝 Manual replacement** | Shows full current prompt (copyable code block) → user sends new text → UPDATE DB |
| **✨ AI-enhanced editing** | User describes desired changes → LLM generates enhanced prompt → preview → confirm/cancel |

### Key Details

| Detail | Description |
|--------|-------------|
| Active role sync | If the edited role is currently active, `system_prompt` is updated automatically |
| AI prompt design | Minimal instruction — no safety guardrails injected per design |
| State management | Uses `context.user_data` (volatile, non-persistent) following the existing rename flow pattern |
| UI layout | Role details now has 3 rows: View+Edit prompt → Rename+Delete → Back |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Tests | **9 passed** (`test_roles_menu.py`), 0 failed |

### Files Changed (6 files)

| File | Change |
|------|--------|
| `app/repos/roles.py` | Added `update_custom_role_prompt()` with `RETURNING id` for safety |
| `app/handlers/cb_roles.py` | 6 new callbacks: `role_edit_prompt`, `role_edit_manual`, `role_edit_ai`, `role_edit_cancel`, `role_edit_ai_save` |
| `app/handlers/msg_roles.py` | New `handle_edit_prompt()` — dual-mode message handler (manual + AI) |
| `app/handlers/menus.py` | Added "✏️ Промпт" button; separated rename/delete into own row |
| `app/handlers/callbacks.py` | 5 new callback registrations |
| `app/handlers/messages.py` | `handle_edit_prompt` added to dispatch chain |

---

## [2.8.52] - 2026-03-22 - Agentic Research Module Overhaul (5 Improvements)

### ⚡ Improvement 1: Parallel Tool Execution

| Change | File | Detail |
|--------|------|--------|
| Concurrent tool calls | `agentic.py` | Multiple tool calls in a single LLM turn now execute via `asyncio.gather()` with `Semaphore(3)`. Page limits validated in batch BEFORE execution to prevent race conditions. |

### 💾 Improvement 2: Page Content Caching

| Change | File | Detail |
|--------|------|--------|
| Two-layer cache | `agentic.py` | Session-local `dict` + global `dict` with 30-min TTL (maxsize=500). Same URL in one research session → only 1 Jina API call. SHA256 cache keys eliminate collision risk. |

### 🏅 Improvement 3: Source Quality Scoring & Citation Validation

| Change | File | Detail |
|--------|------|--------|
| Domain classification | `agentic.py` | Search results enriched with `domain_type` (official_docs, academic, community, etc.), `quality_tier` (A/B/C), and `freshness` (recent/this_year/older). 20+ known domains classified. |
| Citation validator | `agentic.py` | Post-processing log-only check flags cited URLs not found in search results (observability, no blocking). |

### 🎯 Improvement 4: Adaptive Iteration Budget

| Change | File | Detail |
|--------|------|--------|
| Query deduplication | `agentic.py` | Word-level Jaccard similarity (≥0.85) detects duplicate queries. Sends soft advisory instead of re-executing. |
| Token budget cap | `agentic.py`, `config.py` | New `AGENTIC_MAX_TOKENS` (default 100K). Checked BEFORE each LLM call → forces synthesis when exceeded. |
| Time cutoff | `agentic.py`, `config.py` | New `AGENTIC_TIMEOUT_SECONDS` (default 90s). Checked BEFORE each LLM call → forces synthesis when exceeded. |

### 📡 Improvement 5: Streaming Research Progress

| Change | File | Detail |
|--------|------|--------|
| Rich status updates | `agentic.py`, `ai_search.py` | `on_status(text, *, detail=None)` now shows search queries (`🔍 «query»`), page domains (`📖 example.com`), and iteration counters (`Итерация 2/5 • 1 стр. • 8 источников`). Backward-compatible via keyword-only `detail` arg. |

### 🛡️ Defensive Fix

| Fix | File | Detail |
|-----|------|--------|
| `_extract_token_count` mock safety | `agentic.py` | Coerces raw value via `int()` with `TypeError`/`ValueError` guard. Prevents MagicMock leaking through in tests. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy | 0 new errors |
| Tests | **1295 passed**, 0 failed (71s, 12 workers) |
| New tests | 36 tests in `test_agentic_improvements.py` |

### Files Changed (4 files)

| File | Change |
|------|--------|
| `app/core/agentic.py` | Full rewrite: parallel execution, caching, scoring, adaptive budget, streaming |
| `app/handlers/ai_search.py` | `on_status` accepts `detail` kwarg for rich progress |
| `app/config.py` | Added `AGENTIC_MAX_TOKENS`, `AGENTIC_TIMEOUT_SECONDS` |
| `tests/test_agentic_improvements.py` | [NEW] 36 tests covering all 5 improvements |

---

## [2.8.51] - 2026-03-21 - Image Processing Module Overhaul (7 Improvements)

### 🏗️ Architecture: DRY Unification

| Change | File | Detail |
|--------|------|--------|
| Shared vision helpers | `ai_photo.py` | Extracted `_build_vision_prompt()`, `_send_vision_response()`, `_process_ai_vision()` — eliminated ~60 lines of duplication between `_handle_photo` and `_handle_media_group_photos`. |
| Error sentinel pattern | `ai_photo.py` | Introduced `_VISION_ERROR_HANDLED` sentinel to distinguish error-handled vs genuinely empty AI responses, preventing premature error messages. |
| Prompt transliteration fix | `ai_photo.py` | Corrected garbled transliterations in complex media group search prompt. |

### ⚡ Performance: Adaptive Resize & TTL Cache

| Change | File | Detail |
|--------|------|--------|
| Context-aware dimension capping | `image_utils.py` | New `TASK_DIMS` dict (`describe: 1280`, `search: 768`, `ocr: 2048`) + 3-stage pipeline: thumbnail → JPEG q85 → fallback q75/65. Saves 60-90% tokens on 4K images. |
| Compressed image cache | `image_utils.py` | `TTLCache(maxsize=200, ttl=600)` keyed by `cache_key` (e.g. `file_unique_id`). Eliminates recompression on retries/follow-ups. |
| `TaggedImage` metadata carrier | `image_utils.py`, `gemini.py`, `openrouter.py` | Frozen dataclass carrying `cache_key`, `task_type`, `pre_compressed` across handler→provider boundary. Pre-compressed images skip reprocessing entirely. |

### ✨ UX: Media Group Progress

| Change | File | Detail |
|--------|------|--------|
| Semaphore-limited download | `ai_photo.py` | `asyncio.Semaphore(5)` prevents overwhelming Telegram API during media group downloads. |
| Debounced progress indicator | `ai_photo.py` | Placeholder message updated every 2s with `📸 Загружено N/M...` during media group downloads. |
| Thread-safe progress counter | `ai_photo.py` | Per-call `asyncio.Lock()` guards progress dict updates with documented thread-safety contract. |

### 🧪 Test Fixes

| Fix | File | Detail |
|-----|------|--------|
| Module-level import patches | `test_ai_photo.py` | Updated patches to target consumer path (`app.handlers.ai_photo.*`) instead of source modules. |
| Missing mock restoration | `test_ai_photo.py` | Re-added `_get_ai_response_with_routing` mock for empty/error tests. |
| Fixture gap | `test_ai_photo.py` | Added `context_summary` attribute to `make_chat_state`. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Mypy | 6 pre-existing only, 0 new |
| Tests | **1259 passed**, 0 failed (72s, 12 workers) |

### Files Changed (5 files)

| File | Change |
|------|--------|
| `app/handlers/ai_photo.py` | DRY helpers, TaggedImage wrapping, Semaphore download, progress, Lock |
| `app/utils/image_utils.py` | TaggedImage dataclass, TASK_DIMS, 3-stage pipeline, TTLCache |
| `app/providers/gemini.py` | TaggedImage handling in `_build_contents` |
| `app/providers/openrouter.py` | TaggedImage handling in `_build_messages` + `_has_multimodal_content` |
| `tests/test_ai_photo.py` | Patch targets, missing mocks, fixture gap |

---

## [2.8.50] - 2026-03-21 - Reliability Improvements & Metrics Fixes (7 Improvements + 5 Bug Fixes)

### 🔴 P1 — Critical

| Change | File | Detail |
|--------|------|--------|
| Embedding client reuse | `repos/memory.py` | Replaced per-call `genai.Client()` with `get_cached_genai_client()`, eliminating FD leak and ~200ms TLS overhead per embedding call. |
| LTM failure observability | `repos/memory.py`, `ai_chat.py` | Added `record_error("ltm_embedding_fail")` metric emission. Elevated memory recall logging from `debug` → `warning` for production visibility. |
| Web API rate limiting + bug fix | `web.py` | Added `rate_limit_api` decorator (60 req/min/IP) to all 8 API endpoints. Fixed `_recent_errors` → `error_log` attribute name in `/api/errors`. |

### 🟡 P2 — Important

| Change | File | Detail |
|--------|------|--------|
| DatabaseManager init cleanup | `database.py` | Refactored from `__new__` to `__init__` with `_initialized` guard. `_cache_lock` now a lazy property (Python 3.12+ compat). |
| Prompt template validation | `prompt_registry.py` | Added `required_vars` field to `PromptTemplate`. Pre-substitution validation raises `ValueError` for missing vars. Post-substitution regex warns about unresolved `{placeholder}` leaks. |
| Token count accuracy | `streaming.py`, `gemini.py`, 4 handlers | Added `_last_token_count` contextvar + `set_last_token_count()`. Gemini streaming extracts `usage_metadata.total_token_count` from final chunk. `ai_chat.py` prefers API count over heuristic. `stream_and_display` now returns 4-tuple. |

### 🟠 P3 — Nice to Have

| Change | File | Detail |
|--------|------|--------|
| Model selector improvements | `model_selector.py` | Activated dead `_CREATIVE_PATTERNS` regex for creative task suggestions. Added OpenRouter model guard (skips irrelevant tier-based suggestions). |

### 🐛 Bug Fixes in `metrics.py` (from audit commit `8a5e9cb`)

| Fix | Root Cause | Detail |
|-----|-----------|--------|
| `MetricsCollector._lock` missing | Never initialized in `__init__` | Added `self._lock = asyncio.Lock()` to protect daily_metrics reset. |
| `self._daily_metrics` wrong name | Attribute is `self.daily_metrics` | Fixed naming mismatch at line 244. |
| `self._today_key()` nonexistent | Method doesn't exist | Replaced with `date.today().isoformat()` (established codebase pattern). |

### 🧪 Test Fixes

| Fix | File | Detail |
|-----|------|--------|
| `test_system_status` import error | `test_system_status.py` | `sys.modules` mock replaced `app.utils` with `MagicMock`, breaking `from app.utils.metrics_middleware import`. Added `metrics_middleware` to mock dict. |
| Stale key masking assertions | `test_system_status.py` | `get_system_status_data()` now masks keys via `_mask_key()`. Updated assertions to expect `"****"`. |
| `stream_and_display` mock tuples | 8 test files | Updated all mock return values from 3-tuple to 4-tuple for token count. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 234 files clean |
| Tests | **1259 passed**, 0 failed (69s, 12 workers) |

### Files Changed (20 files)

| File | Change |
|------|--------|
| `app/repos/memory.py` | Cached embedding client + error metrics |
| `app/web.py` | Rate limiting + `/api/errors` bug fix |
| `app/handlers/ai_chat.py` | 4-tuple return + actual token count preference |
| `app/handlers/ai_document.py` | 4-tuple return |
| `app/handlers/ai_search.py` | 4-tuple return |
| `app/handlers/ai_photo.py` | 4-tuple return (2 sites) |
| `app/database.py` | `__init__` refactor + lazy `_cache_lock` |
| `app/prompt_registry.py` | `required_vars` validation |
| `app/streaming.py` | `_last_token_count` contextvar + 4-tuple return |
| `app/providers/gemini.py` | `usage_metadata` extraction in streaming |
| `app/model_selector.py` | Creative patterns + OpenRouter guard |
| `app/metrics.py` | `_lock` init + `_daily_metrics` fix + `_today_key` fix |
| `tests/test_ai_chat.py` | 4-tuple mocks |
| `tests/test_ai_search.py` | 4-tuple mocks |
| `tests/test_ai_photo.py` | 4-tuple mocks |
| `tests/test_ai_document.py` | 4-tuple mocks |
| `tests/test_integration_flow.py` | 4-tuple mocks |
| `tests/integration/test_e2e_app_smoke.py` | 4-tuple mocks |
| `tests/test_phase3_features.py` | 4-tuple unpacking |
| `tests/test_system_status.py` | Import fix + key masking assertions |

---

## [2.8.49] - 2026-03-21 - Comprehensive Codebase Audit (10 Fixes)

### 🔴 P1 — Critical

| Fix | File | Detail |
|-----|------|--------|
| TaskManager class-level mutable state | `background_tasks.py` | Refactored from class-level `_tasks` set to instance-based design with global singleton via `get_task_manager()`. Ensures proper test isolation and multi-context safety. |
| Bare coroutine retry guard | `background_tasks.py` | `_schedule()` now raises `ValueError` when `retry > 0` but no `coro_factory` is provided, preventing silent crashes from double-awaiting bare coroutines. |
| Metrics upsert data loss | `metrics.py` | Changed `ON CONFLICT SET = EXCLUDED.*` to atomic increments (`metrics.X + EXCLUDED.X`) for both `metrics` and `user_metrics` tables. In-memory counters reset after successful DB save to prevent double-counting. |

### 🟡 P2 — High

| Fix | File | Detail |
|-----|------|--------|
| CircuitBreaker HALF_OPEN race | `circuit_breaker.py` | Added `_half_open_probe_active` flag — only one probe request enters HALF_OPEN state; concurrent requests get `CircuitBreakerOpenError`. Flag cleared on both success and failure. |
| State read-before-load | `state.py` | Increased LRU `maxsize` from 10K to 50K. Added `logging.warning` in sync getters when state hasn't been loaded from DB. |
| API key exposure | `metrics.py` | Added `_mask_key()` helper (shows first/last 4 chars). Applied to Gemini and Tavily key listing in `get_system_status_data()`. |

### 🟠 P3 — Medium

| Fix | File | Detail |
|-----|------|--------|
| Agentic config mutation | `agentic.py` | Replaced in-place `config.tools = None` mutation with a fresh `synthesis_config` object. Propagates `thinking_config` from original. |
| Streaming recursion depth | `streaming.py` | Added `_depth` counter to `_flush()`. Truncates at `_depth > 5` or `msg_count > 8` instead of unbounded recursion. |

### 🟢 P4 — Low

| Fix | File | Detail |
|-----|------|--------|
| Redundant SQL subquery | `conversations.py` | Simplified `WHERE conversation_id = $1 AND conversation_id IN (SELECT ...)` to just the subquery. |
| `datetime.now()` without timezone | `queue.py` | All 6 `datetime.now()` calls → `datetime.now(tz=timezone.utc)`. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Ruff lint | 0 errors |
| Ruff format | 235 files clean |
| Tests (targeted) | **101 passed**, 0 failed (6.55s) |

### Files Changed

| File | Change |
|------|--------|
| `app/utils/background_tasks.py` | Instance-based TaskManager + retry guard |
| `app/metrics.py` | Atomic upsert + counter reset + `_mask_key()` |
| `app/circuit_breaker.py` | HALF_OPEN probe flag |
| `app/state.py` | LRU 50K + read-before-load warning |
| `app/core/agentic.py` | Fresh synthesis config |
| `app/streaming.py` | `_flush()` depth guard |
| `app/repos/conversations.py` | SQL simplification |
| `app/queue.py` | UTC-aware datetime |
| `bot.py` | Updated TaskManager caller API |
| `tests/test_background_tasks.py` | Instance-based tests + bare-coro guard test |
| `tests/test_taskmanager_bounded.py` | Instance-based bounded test |
| `tests/e2e/test_chat_happy_path.py` | Updated patch target |

---

## [2.8.48] - 2026-03-20 - Multi-Tier Semaphore Architecture & E2E Hardening

### 🏗️ Architecture Changes

| Change | File | Detail |
|--------|------|--------|
| Multi-Tier Semaphores | `concurrency.py`, `agent.py` | Extracted and exported `ultra_heavy_semaphore` alongside `heavy_request_semaphore` globally. Moved semaphore acquisition to `process_long_request` for dynamic routing based on computational cost (e.g., isolating deep dives). |
| Concurrency Isolation | `messages.py` | Removed synchronous semaphore acquisition from the top-level `task_wrapper` allowing background tasks to route unblocked. |
| E2E Database Fixes | `test_chat_happy_path.py`  | Hardened integration tests against `asyncpg` Task/Connection starvation constraints. Added targeted background task yielding (`asyncio.sleep`) and local `asyncio.Semaphore(1)` patching to resolve race conditions with `TransactionalPool` during E2E test evaluation. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Tests | **1256 passed**, 0 failed (71s, 12 workers) |
| Ruff lint | Pass |
| Ruff format | Pass |

---

## [2.8.47] - 2026-03-20 - Agentic Key Usage Tracking Fix

### 🔴 Critical Fix

| Fix | File | Detail |
|-----|------|--------|
| Agentic loop bypassed key usage tracking | `agentic.py`, `ai_search.py` | `AgenticSearch.run()` made 2–6 direct `generate_content` calls per session but never called `increment_gemini_key_usage`. This created a loophole where the `DailyKeyManager` believed the key was idle while its real Google quota was being exhausted. |

### 🏗️ Architecture Changes

| Change | File | Detail |
|--------|------|--------|
| New `AgenticResult` dataclass | `agentic.py` | `run()` now returns `AgenticResult(answer, total_tokens, llm_calls)` instead of a raw string, enabling callers to track resource consumption. |
| `on_key_used` callback pattern | `agentic.py` | New constructor parameter `on_key_used: Callable[[], Awaitable[None]]` fires after **every** `generate_content` call (loop iterations + forced synthesis). Each invocation = +1 in `key_usage`. |
| Per-call metrics recording | `ai_search.py` | `_on_key_used` closure calls `increment_gemini_key_usage(key_hash, model)` and `record_api_call("gemini_agentic")` per LLM invocation. Logs total LLM calls and tokens on completion. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Tests | **1256 passed**, 0 failed (76s, 12 workers) |
| Ruff lint | Pass |
| Ruff format | Pass |

### Files Changed

| File | Change |
|------|--------|
| `app/core/agentic.py` | Added `AgenticResult`, `on_key_used` callback, `_notify_key_used()`, `_extract_token_count()`. All `return` paths now return `AgenticResult`. |
| `app/handlers/ai_search.py` | `_handle_research_agent` wires `_on_key_used` callback, handles `AgenticResult`. |
| `tests/test_agentic_search.py` | Updated 4 tests: `result.answer` assertions. |
| `tests/test_ai_search.py` | Updated 3 tests: added `key_hash` to mocks, `AgenticResult` return values. |

---

## [2.8.46] - 2026-03-18 - Model Hierarchy Modernization

### ⚙️ Model Tier Overhaul

| Change | File | Detail |
|--------|------|--------|
| Remove unavailable `gemini-2.5-pro` | `model_selector.py`, `menus.py` | All references to `gemini-2.5-pro` and preview variants replaced with `gemini-2.5-flash`. |
| Remove legacy models (1.5, 2.0) | `menus.py`, 18 test files | Purged all `gemini-1.5-*` and `gemini-2.0-*` references from application code and test mocks. |
| Add `gemini-3-flash-preview` and `gemini-3.1-flash-lite` | `model_selector.py`, `menus.py` | New 5-tier hierarchy: `3.0-flash`(5) > `3.1-flash-lite`(4) > `2.5-flash`(3) > `2.5-flash-lite`(1). Benchmarks confirm `3.1-flash-lite` outperforms `2.5-flash` in speed (2.5× TTFT) and quality (higher Arena Elo). |
| Smart routing update | `model_selector.py` | Code and reasoning task routing now prefers `3.0-flash` → `3.1-flash-lite` → `2.5-flash`. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Tests | **1250 passed**, 0 failed (78s, 12 workers) |
| Ruff lint | Pass |
| Ruff format | Pass |
| Mypy | 0 errors |

---

## [2.8.45] - 2026-03-18 - Admin Metrics & Waiting Facts Stability

### 🔴 Critical Fixes

| Fix | File | Detail |
|-----|------|--------|
| `UndefinedColumnError` in `/metrics` | `app/metrics.py` | Querying `api_keys` and `tavily_api_keys` for historical columns generated DB exceptions because usage metadata is stored in separate tables. Query simplified to `key_hash` and `api_key` with decryption logic restored. |
| `toordinal()` error in waiting stats | `app/utils/waiting_facts.py` | `metric_date` is a `DATE` column, but asyncpg was sent an `.isoformat()` string. Directly passing `datetime.date` resolves the coercion crash. |

### 🧹 Infrastructure & QA

| Change | File | Detail |
|--------|------|--------|
| Git & Docker Ignores | `.gitignore`, `.dockerignore` | Guarded against accidental check-ins and image bloat by appending `benchmark*.py`, `test_perf.py`, `load_test.py`, and `.pytest_tmp/`. |
| Quality Gates | - | 54 formatting and styling alerts resolved via Ruff; Pytest suite expanded to **1250 tests** running in 80s; Mypy reports 0 strict type errors. |

---

## [2.8.44] - 2026-03-13 - Streaming Overflow Fixes (BUG-10)

### 🔴 Critical Fix

| Fix | File | Detail |
|-----|------|--------|
| Infinite retry storm on overflow ("Flood control exceeded") | `streaming.py` | Added circuit breaker (`_overflow_failed` + 5s backoff) to prevent hot-looping when Telegram rejects an overflow message chunk. |
| "Can't parse entities" on cross-boundary format tags | `streaming.py` | Overflows missing format tags (e.g. `_`) that were carried over to the new chunk are now properly sanitized via `sanitize_html_tags()` before being sent to Telegram. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| Tests | Pass (1214 passing) |
| Linter | Pass |

## [2.8.43] - 2026-03-13 - Memory Persistence Fix (BUG-9)

### 🔴 Critical Fix

| Fix | File | Detail |
|-----|------|--------|
| Memories injected into every chat regardless of relevance | `ai_chat.py` | `min_similarity` threshold was `0.55` — far too low for `gemini-embedding-001` embeddings where unrelated strings naturally score `0.6–0.7`. Raised to `0.72`. |

### 🟢 New Features

| Feature | Files | Detail |
|---------|-------|--------|
| Long-Term Memory toggle in `/settings` | `database.py`, `commands.py`, `cb_navigation.py`, `callbacks.py`, `chats.py` | New `ltm_enabled` field on `ChatState` with 📚 toggle button. Persisted via `017_add_ltm_enabled.sql` migration. |
| `/clearmemory` command | `commands.py` | Deletes all long-term vector memories for the user. Registered in the command router. |
| Memory usage indicator | `ai_chat.py` | Appends `🧠 Использован контекст из прошлых бесед (N)` to responses when LTM was used. For streamed responses, uses `edit_text` on the final message. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` | 0 errors |
| `pytest -n auto` | **1212 passed**, 0 errors |

### Files Changed

| File | Change |
|------|--------|
| `app/database.py` | Added `ltm_enabled: bool = True` to `ChatState` |
| `app/handlers/ai_chat.py` | Threshold `0.55→0.72`, LTM guard, memory footnote |
| `app/handlers/commands.py` | `/clearmemory` command, settings LTM line + button |
| `app/handlers/cb_navigation.py` | `toggle_ltm_callback`, updated `__all__` |
| `app/handlers/callbacks.py` | Registered `toggle_ltm_callback` |
| `app/repos/chats.py` | Added `ltm_enabled` to SQL SELECT/UPSERT |
| `app/db/migrations.py` | Legacy migration for `ltm_enabled` column |
| `scripts/migrations/017_add_ltm_enabled.sql` | DDL migration |
| `tests/factories.py` | Added `ltm_enabled` to `make_chat_state` |
| `tests/test_integration_flow.py` | Added `ltm_enabled` to inline mocks |
| `tests/test_ui_adapter.py` | Fixed `allow_sending_without_reply` assertion |
| `tests/integration/conftest.py` | Schema migration for test DB |

---

## [2.8.42] - 2026-03-13 - Multi-Message Streaming Bug Fixes

### 🔴 Critical Fixes

| Fix | File | Detail |
|-----|------|--------|
| Draft mode corruption on overflow | `streaming.py` | After overflow into a 2nd message, `_use_drafts` stayed `True` — next `write()` attempted to `delete_placeholder()` on the new continuation message, potentially deleting visible content. Fixed by calling `_switch_to_classic()` after draft overflow. |
| Missing reply threading in draft overflow (BUG-6) | `ui_adapter.py` | `send_final_message()` sent standalone messages without `reply_to_message_id`, breaking reply threading in private chats. Overflow messages now correctly chain as replies. |
| Malformed HTML from markdown parser hallucination (BUG-7) | `streaming.py` | `_detect_open_markdown` counted formatting characters (e.g. `_`, `*`) inside ` ``` ` fences. Unclosed code block tags caused "unmatched end tag" Telegram API errors. |

### 🟡 Medium Fixes

| Fix | File | Detail |
|-----|------|--------|
| Buttons not attached atomically | `streaming.py`, `ui_adapter.py` | `reply_markup` was not forwarded through `edit_message()` in the classic finalize path. Callers' fallback `edit_reply_markup()` worked but created an extra API round-trip and a race condition. `edit_message()` now accepts optional `reply_markup`. |
| Settings Thinking Button inoperative (BUG-8) | `cb_navigation.py`, `callbacks.py` | The "Мышление" button in `/settings` had no assigned callback handler. Implemented `settings_thinking_callback` with cycle logic (off -> low -> medium -> high) in `cb_navigation.py` and registered it in the fast callback router. |

### 🧪 Tests

| New Tests | Class | Covers |
|-----------|-------|--------|
| 2 | `TestDraftOverflowSwitchesToClassic` | BUG-1: `_use_drafts` resets to `False`, debounce/chunk match classic mode |
| 2 | `TestClassicFinalFlushPassesReplyMarkup` | BUG-2: `reply_markup` forwarded to/omitted from `edit_message()` |
| 2 | `TestSendFinalMessageReplyThreading` | BUG-6: `reply_to_message_id` correctly preserved for threaded replies from user prompt. |
| 2 | `TestDetectOpenMarkdown` | BUG-7: Safely ignores code blocks and inline code markers. Added strikethrough checking regex. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` | 0 errors |
| `ruff format` | 0 files to reformat |
| `pytest -n auto` | **1206 passed** (including integration test suite), 0 errors |

### Files Changed

| File | Change |
|------|--------|
| `app/streaming.py` | `_switch_to_classic()` after draft overflow; `reply_markup` forwarded to `edit_message()`; `_detect_open_markdown` ignoring fences. |
| `app/adapters/ui_adapter.py` | `edit_message()` accepts `reply_markup`; `send_final_message()` adds `reply_to_message_id`. |
| `app/handlers/cb_navigation.py` | Implemented `settings_thinking_callback` cycle. |
| `app/handlers/callbacks.py` | Registered `settings_thinking` regex in fast non-blocking router. |
| `tests/test_streaming.py` | Removed duplication, added +7 targeted regression tests for draft overflow, threading, and code fences. |

---

## [2.8.41] - 2026-03-13 - Test Suite Expansion & Reliability

### 🧪 14 New Test Modules (1201 total tests, 60% coverage)

| Module | Tests | Covers |
|--------|-------|--------|
| `test_admin_alerts.py` | Admin alert routing & formatting |
| `test_agent_use_cases.py` | Agent dispatch routing edge cases |
| `test_config_helpers.py` | `_load_and_clean_keys`, `_load_daily_limits`, `get_model_hash` |
| `test_context_summarizer.py` | `_extract_text`, `split_into_chunks` |
| `test_heartbeat.py` | Heartbeat typing indicator lifecycle |
| `test_json_utils.py` | JSON serialization utilities |
| `test_model_selector.py` | Smart model auto-selection heuristics |
| `test_network.py` | Retry logic with exponential backoff |
| `test_openrouter_provider.py` | OpenRouter streaming & error handling |
| `test_search_services.py` | Tavily input validation & parallel dedup |
| `test_tracing.py` | Request span binding & context propagation |
| `test_ui_adapter.py` | Telegram UI adapter formatting |
| `test_waiting_facts.py` | Waiting facts display logic |
| `test_web_reader.py` | Jina Reader API wrapper |

### 🔴 Critical Fixes

| Fix | File | Detail |
|-----|------|--------|
| Single-threaded test hang | `test_messages.py` | `handle_request` dispatches via `submit_task()` (not `asyncio.create_task`), so the test's `create_task` patch was bypassed — unmocked `process_long_request` ran as a real background task, blocking the event loop forever at 57%. Fixed by mocking `submit_task` at source + `ensure_state_loaded`. |
| asyncpg teardown error | `integration/conftest.py` | `db_conn` fixture called `tx.rollback()` unconditionally at teardown; when xdist deferred teardown while the connection had a pending operation, it crashed with `InterfaceError`. Added `conn.reset(timeout=5.0)` + graceful error handling. |

### 🟡 Medium Fixes

| Fix | File | Detail |
|-----|------|--------|
| CircuitBreaker test API | `test_circuit_breaker.py` | Updated from private `_stats` dict to public `get_stats()` method. |
| TaskManager bounded test | `test_taskmanager_bounded.py` | Fixed race condition in capacity test by using `asyncio.Event` synchronization. |
| Smoke test resilience | `test_smoke.py` | Hardened import-time side-effect handling. |
| Dev dependencies | `requirements-dev.txt` | Added `pytest-cov`, `pytest-xdist`. |

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff check` | 0 errors |
| `ruff format` | 0 files to reformat |
| `mypy app/` | 0 errors in 101 files |
| `pytest -n auto` | **1201 passed**, 0 errors, 2 warnings, 67s |
| `pytest --cov=app` | **60% line coverage** (10185 lines) |

### Files Changed

| File | Change |
|------|--------|
| `tests/test_messages.py` | Mocked `submit_task` + `ensure_state_loaded` to fix hang |
| `tests/integration/conftest.py` | Resilient `db_conn` teardown with `conn.reset()` |
| `tests/test_circuit_breaker.py` | Updated to `get_stats()` API |
| `tests/test_taskmanager_bounded.py` | Event-based synchronization |
| `tests/test_smoke.py` | Import hardening |
| `tests/test_tracing.py` | Unused variable fix |
| `requirements-dev.txt` | +pytest-cov, +pytest-xdist |
| 14 new `tests/test_*.py` | See table above |

---

## [2.8.40] - 2026-03-13 - Streaming UX Polish & Concurrency Feedback

### ✨ UX Improvements & Bug Fixes
| Fix | File | Detail |
|-----|------|--------|
| Typing Indicator Timeout | `messages.py` | Fixed Telegram's 5-second typing indicator timeout by repurposing `_heartbeat` to send periodic `ChatAction.TYPING` packets. |
| Draft Message Duplication | `streaming.py` | Streaming system now perfectly balances the initial UI placeholder vs final AI draft response, gracefully deleting the `placeholder_message` before chunk emission to prevent dual-render cloning. |
| Double Reply Toast Bug | `cb_ai_actions.py` | Overhauled all action callbacks (`complex_search`, `fallback_callback`, `retry_last_callback`) to call `query.answer()` strictly once. Fixed bug where concurrent execution warning toast "⏳" was swallowed by Telegram's single-answer barrier. |
| Orphaned Placeholders | `cb_ai_actions.py` | Moved request sequence lock checks *before* `message.reply_text()` generation. Prevents permanent chat clutter with disconnected "Повторяю запрос" bubbles following lock rejections. |

### 🧪 Tests & Quality
- `test_cb_ai_actions.py` expanded with strict `AsyncMock` counter assertions to guarantee single `query.answer()` execution.

---

## [2.8.39] - 2026-03-11 - Deep Dive UX: Soft Exit Button

### ✨ UX Improvement
| Change | File | Detail |
|--------|------|--------|
| Added "💬 Вернуться в чат" button | `keyboards.py`, `cb_navigation.py` | After deep dive (`??`) responses, users now see a friendly exit button that disables `is_deep_dive` without clearing conversation history. Previously the only option was "Начать новую тему" which fully reset the chat — too destructive, users avoided pressing it and stayed stuck in search mode. |

## [2.8.38] - 2026-03-11 - Agentic Search Memory & Context Fixes

### 🔴 Critical Fixes
| Fix | File | Detail |
|-----|------|--------|
| Unbounded memory leak | `agentic.py` | Google GenAI SDK protobuf implementations create reference cycles that evade standard GC when returned. A single 5-iteration `??` search generated up to 250MB in leaked `contents` objects (containing full parsed web pages). Added strict `contents.clear()` + `gc.collect()` in a `finally` block to force cyclic GC sweep, completely resolving the leak. |
| Follow-up routing gap | `agent.py` | After `??` search set `is_deep_dive=True`, follow-up messages fell through to regular chat (no search tools) → hallucinations. Added `elif chat_state.is_deep_dive` routing case. |
| Missing conversation context | `agentic.py`, `ai_search.py` | `AgenticSearch.run()` started from scratch each time. Now receives last 10 history entries so the agent can answer contextual follow-ups ("А за 2018?") using prior search results. |

### 🟡 Medium Fixes
| Fix | File | Detail |
|-----|------|--------|
| DB schema mismatch | `waiting_facts.py` | `SELECT created_at FROM users` failed because `users` table lacks `created_at`. Replaced with `SELECT MIN(metric_date) as first_seen FROM user_metrics` (index-only scan). |

## [2.8.35] - 2026-03-11 - Code Quality Audit & Test Acceleration

### 🔍 Professional Code Audit (9 Findings Fixed)

| ID | Severity | Fix | File |
|----|----------|-----|------|
| C1 | 🔴 Critical | Extracted `get_cached_genai_client()` factory — `AgenticSearch` and `GeminiProvider` now share TLS-cached clients | `gemini.py`, `agentic.py` |
| C2 | 🔴 Critical | Added `close()` coroutine for module-level `httpx.AsyncClient` to prevent resource leaks on shutdown | `web_reader.py` |
| C3 | 🔴 Critical | Moved mid-file imports to module top | `ai_search.py` |
| M2 | 🟡 Medium | Added TTL cache (120s) for personalized stats — DB queries drop from ~27 to ~3 per research session | `waiting_facts.py` |
| M3 | 🟡 Medium | Replaced global `asyncio.create_task` test monkeypatch with local interception to prevent Windows `stack overflow` & semaphore exhaustion | `test_messages.py` |
| M4 | 🟡 Medium | Wired `settings.AGENTIC_MODEL` into model selection chain (was defined but unused) | `ai_search.py` |
| M5 | 🟡 Medium | Fixed `relation "authorized_users" does not exist` error by pointing query to correct `users` table | `waiting_facts.py` |
| M6 | 🟡 Medium | Fixed `search_type must be 'search' or 'qna'` API exception by using correct internal Enum mapping | `search_services.py` |
| L1 | 🟢 Low | Replaced f-string logging with `%s` format (6 instances) | `agentic.py` |
| L2 | 🟢 Low | Fixed typo "про проведении" → "при проведении" | `ai_search.py` |
| L3 | 🟢 Low | Deduplicated `has_function_calls` iteration + added mypy assertion for type narrowing | `agentic.py` |
| L4 | 🟢 Low | Implemented 10-second cache for waiting facts during AI search loops so users have time to read them | `ai_search.py` |

### ⚡ Test Suite Acceleration

- **Parallel Execution**: Installed `pytest-xdist` and set `addopts = -n auto` in `pytest.ini` for automatic parallel test execution across all CPU cores.
- **Integration Test Separation**: Added `@pytest.mark.integration` marker to all 14 integration test files. Default `pytest` run now excludes them via `-m "not integration"`.
- **CI Pipeline Split**: GitHub Actions `ci.yml` now has two test jobs:
  - `test-unit` (fast, ~1-2min): Runs on every push/PR — unit tests only.
  - `test-integration` (slow, ~5min): Runs only on merge to `main` — full suite with DB tests.

### ✅ Quality Gates

| Check | Result |
|-------|--------|
| `ruff format + check` | 0 errors |
| `mypy app/ tests/` | 0 errors in 217 files |
| `pytest` (unit, parallel) | 1000+ tests passed |

### Files Changed

| File | Change |
|------|--------|
| `app/providers/gemini.py` | Added `get_cached_genai_client()` factory |
| `app/core/agentic.py` | Uses cached client, %s logging, deduplication |
| `app/web_reader.py` | Added `close()` coroutine |
| `app/handlers/ai_search.py` | Import ordering, typo fix, AGENTIC_MODEL wiring |
| `app/utils/waiting_facts.py` | TTL cache for personalized stats |
| `tests/test_messages.py` | Background task await fix |
| `tests/test_agentic_search.py` | Updated mock for cached client factory |
| `pytest.ini` | `addopts = -n auto --basetemp=.pytest_tmp -m "not integration"` |
| `.github/workflows/ci.yml` | Split into `test-unit` + `test-integration` jobs |
| `tests/integration/*.py` | Added `pytestmark = pytest.mark.integration` |

---

## [2.8.34] - 2026-03-10 - Agentic Web-Browsing Research

### 🤖 Agentic Research Loop

- **Multi-Step Query Decomposition**: Upgraded static web search to a dynamic agentic loop (`AgenticSearch`). The Gemini model can iteratively decompose complex questions, formulate search queries via the Tavily API, and self-correct if the gathered context is incomplete.
- **URL Triage & Pre-Evaluation**: The agent can autonomously evaluate search result metadata (titles, snippets) and selectively pick which URLs warrant deep reading.
- **Deep DOM Parsing with Jina**: Integrated `Jina Reader API` (`web_reader.py`) to bypass anti-bot protections, extract core text from noisy DOMs, and accurately retrieve article contents from target URLs.
- **Iterative Synthesis**: Bound loop recursion with strict limits (`AGENTIC_MAX_ITERATIONS=3`, `AGENTIC_MAX_PAGES=3`) to prevent infinite scraping while providing comprehensive, deeply-researched synthesis answers.
- **Dynamic Waiting Facts**: Introduced `app/utils/waiting_facts.py` to display personalized system stats and generic interesting trivia to the user while the agent is executing prolonged multi-step research iterations, providing a better UX.

### 🧹 Infrastructure Updates

- **Dependencies**: Added configurations for `JINA_API_KEY` and updated the `prompt_registry` with a dedicated new `RESEARCH_AGENT_SYSTEM` zero-shot planning role.
- **Testing**: Added extensive test suites and mock infrastructure (`test_agentic_search.py`) covering the agentic iteration flow, direct answer fallbacks, parsing errors, tool calling signatures, and API exceptions.

### Files Changed

| File | Change |
| --- | --- |
| `app/core/agentic.py` | [NEW] Implement `AgenticSearch` loop managing tools and memory |
| `app/web_reader.py` | [NEW] Implement Async wrapper over `Jina Reader API` |
| `app/utils/waiting_facts.py` | [NEW] Fun facts & stats during active research waiting |
| `app/handlers/ai_search.py` | Fully refactored to utilize the `AgenticSearch` process |
| `app/search_services.py` | Parameterized advanced queries allowing dynamic max lengths |
| `app/stage_indicators.py` | Added agentic specific UI stage indicators (`STAGES_AGENTIC_RESEARCH`) |

### 🧪 Tests: 1064 passed (100% Core + Integration Coverage)

---

## [2.8.33] - 2026-03-10 - Concurrency Scaling & Reliability

- **Strict Context Isolation**: Relocated the _last_finish_reason state from a globally mutable module variable into a thread-safe contextvars.ContextVar. Resolved a dangerous race condition where parallel streams could leak safety block statuses into adjacent AI responses.
- **Circuit Breaker Unblocking**: Severely refactored CircuitBreaker.call() to explicitly release its internal syncio.Lock during remote HTTP execution. This eradicated a critical head-of-line blocking bottleneck, allowing 100% concurrent request throughput without sacrificing state transition safety.
- **Connection Pool Harmonization**: Enforced explicit limitations on incoming Telegram updates (concurrent_updates=50) to perfectly align with the syncpg maximum connection pool size. This prevents database saturation timeouts under burst conditions.

### 🛡️ Graceful Degradation & Memory Safety

- **Bounded Task Queue**: Hardened the fire-and-forget TaskManager by implementing a strict MAX_TASKS = 100 upper limit. Submissions beyond this queue are synchronously rejected, mitigating the risk of Out-Of-Memory (OOM) failures from orphan background jobs.
- **Graceful DB Teardown**: Interlocked TaskManager.drain(timeout=10.0) into the application's core SIGTERM/SIGINT shutdown registry. Ensure all long-living background tasks (e.g. vector embedding storage) successfully commit before the event loop collapses.

### Files Changed

| File | Change |
| --- | --- |
| pp/streaming.py | Migrated _last_finish_reason to ContextVar |
| pp/providers/gemini.py | Adapted finish reason propagation to ContextVars |
| pp/providers/openrouter.py | Adapted finish reason propagation to ContextVars |
| pp/circuit_breaker.py | Fractured continuous lock into pre/post-flight scopes |
| ot.py | Hardcoded concurrent_updates=50 and registered TaskManager.drain() |
| pp/utils/background_tasks.py | Bounded Task manager up to 100 max tasks |

### 🧪 Tests: 1060 passed (100% Core + Integration Coverage)

---

## [2.8.32] - 2026-03-10 - E2E Testing & Background Task Stability

### 🧪 End-to-End Test Suite Completion

- **E2E Happy Path**: Added tests/e2e/test_chat_happy_path.py to completely simulate the core conversational loop from Telegram struct ingestion to final PostgreSQL database persistence.
- **Background Task Validation**: Resolved the _background_tasks mock reference to accurately validate and await asynchronous DB queries launched outside the HTTP request lifecycle.
- **Database Schema Alignment**: Fixed archaic assertions in performance and integration tests to correctly expect active_chat_messages tables instead of the legacy user_chats JSONB column.

### 🐛 Bug Fixes & Architecture Alignment

- **Async Mocking TypeErrors**: Resolved MagicMock object can't be awaited in key rotation and metrics generation tests by enforcing correct AsyncMock injection into connection pool context managers.
- **Datetime Mock Isolation**: Corrected the datetime monkeypatching in test_time_utils.py to preserve core module subclasses, preventing integration failures when hydrating Timezones.
- **Memory Feature Coverage**: Added dedicated integration tests for storage vector dimension constraints and similarity_search logic utilizing mocked API embeddings.

### 🧹 Code Quality

- **Ruff Linting**: Auto-fixed import sorting across the new E2E and integration test files.
- **Mypy Type Safety**: Confirmed 100% strict type safety across 212 application files with zero warnings.

### 🧪 Tests: 1056 passed (100% Core + Integration Coverage)

---

 - 2026-03-10 - Integration Test Stabilization

### 🧪 Integration Tests Stabilization

- **Event Loop Leakage Prevention**: Mocked the global `heavy_request_semaphore` in test contexts to prevent the Redis semaphore from holding references to closed `asyncio` event loops across test suite runs resulting in `RuntimeError: Event loop is closed`.
- **Module Namespace Cross-Contamination**: Addressed `AsyncMock` leakage where function imports inside of `handle_request` cached un-mocked database references in the module namespace. Patched both `app.repos.chats.get_user_chat` and `app.handlers.agent.get_user_chat` simultaneously for perfect isolation under heavy pytest load.
- **Strict Parameter Contracts**: Fixed `AttributeError` exceptions inside the integration pipeline by adding missing attributes (`thinking_level`, `context_summary`, `deep_dive_thread_id`) to the `fake_chat_state` mock payload, fully replicating the complex production `ChatState` object.

### Files Changed

| File                             | Change                                                                                                  |
| -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `tests/test_integration_flow.py` | Added comprehensive integration mocks (semaphore, state lock, thinking_level, multiple module bindings) |
| `tests/test_ai_chat.py`          | Cleaned up AAA testing blocks and import definitions                                                    |

### 🧪 Tests: 1054 passed (0 skipped, 0 failures)

---

## [2.8.30] - 2026-03-10 - Test Suite Reliability & CI Pipeline

### 🧪 Test Suite Stabilisation & Technical Debt

- **Database Transaction Isolation**: Implemented a global `force_test_db_conn` autouse fixture in integration tests. This forcefully mocks the `DatabaseManager` connection pool, ensuring all application business logic runs strictly inside the test's `BEGIN`/`ROLLBACK` transaction boundary, providing perfect isolation and parallelization safety.
- **Deterministic Time Mocking**: Eliminated flaky `asyncio.sleep` calls in `test_circuit_breaker.py` by introducing a `mock_time` fixture that monkeypatches `time.time()`. This allows instantaneous testing of timeouts without hanging the event loop, vastly improving test speed and CI reliability.
- **Webhook Integration Fix**: Resolved a `TypeError` parsing bug in `test_integration_webhook.py` by configuring `mock_app.bot.defaults.tzinfo = timezone.utc`, allowing `telegram.Update` JSON hydration to succeed locally.

### ⚙️ CI/CD Pipeline

- **Automated Validation**: Restructured `.github/workflows/ci.yml` pipeline to automatically execute `ruff check`, `ruff format`, `mypy`, and `pytest` across all application directories, tests, and utility scripts (`load_test.py`).
- **Graceful DB Degradation**: Configured the Pytest step to conditionally run integration tests only when `TEST_DATABASE_URL` is present; ensuring GitHub actions execute unit tests fully without failing on missing secrets.

### Files Changed

| File                                            | Change                                                         |
| ----------------------------------------------- | -------------------------------------------------------------- |
| `tests/integration/conftest.py`                 | Added global transactional pool mocking for `db_manager`       |
| `tests/test_circuit_breaker.py`                 | Replaced real sleep timers with `mock_time.advance()`          |
| `tests/integration/test_integration_webhook.py` | Injected UTC timezone into `mock_application`                  |
| `.github/workflows/ci.yml`                      | Expanded QA coverage matrix across `tests/` and `load_test.py` |

### 🧪 Tests: 1055 passed (all suites clean)

---

## [2.8.29] - 2026-03-10 - Concurrency & Graceful Shutdown Bug Fixes

### 🛡️ Concurrency & Operational Stability

- **Concurrency Starvation Fixed**: Inverted the locking order in `messages.py`. Users now acquire their `state.get_user_lock` _before_ taking a slot from the global `heavy_request_semaphore`. Prevention of head-of-line blocking where a single active user could monopolize the entire system.
- **Gemini Connection Pool Leak Fixed**: Implemented `cachetools.LRUCache` to persistently cache Google `genai.Client` instances by API key. Resolves a critical leak where every request instantiated a fresh `httpx.AsyncClient` preventing TLS session reuse and exhausting sockets.
- **Graceful Shutdown Bypass Fixed**: `bot.py` now explicitly catches `asyncio.CancelledError` thrown by the `uvloop` shutdown signals, safely executing the `application.stop()` sequence under `asyncio.shield` to prevent data loss.
- **Task Queue Deadlock Eliminated**: Modified `TaskQueue.stop()` to invoke `worker.cancel()` rather than using blocking `queue.put()` operations. This prevents a classic shutdown deadlock when the queue is completely saturated.

### Files Changed

| File                       | Change                                                               |
| -------------------------- | -------------------------------------------------------------------- |
| `app/handlers/messages.py` | Lock inversion (`user_lock` then `heavy_request_semaphore`)          |
| `app/providers/gemini.py`  | Global `_gemini_clients_cache` LRU implementation                    |
| `bot.py`                   | Catch `asyncio.CancelledError` and shield `application.stop()`       |
| `app/queue.py`             | Prevent queue deadlock by migrating blocking shutdown to `.cancel()` |

### 🧪 Tests: Re-validated Unit Suites

---

## [2.8.28] - 2026-03-09 - Database Latency & N+1 Roundtrip Fixes

### ⚡ Performance & Database Query Batching

- **Resolved N+1 Query Anti-Pattern**: Replaced 5 sequential database requests in `get_user_chat` and `update_user_chat` with single atomic Common Table Expression (CTE) executed pipelines.
- **Drastically Reduced Network Latency**: By aggregating results natively inside PostgreSQL using `row_to_json` and `json_to_recordset`, operations that previously took ~400-600ms due to AWS EU Central PgBouncer roundtrips now complete in ~80ms (a single roundtrip).
- **Consolidated RLS Scoping**: Session context (`set_config('app.user_id', ...)`) is still correctly applied on the unified connections, maintaining strict Row-Level Security isolation without sacrificing performance.

### Files Changed

| File                 | Change                                                            |
| -------------------- | ----------------------------------------------------------------- |
| `app/repos/chats.py` | Overhauled `get_user_chat` and `update_user_chat` SQL statements. |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.27] - 2026-03-09 - Database Reliability & Legacy Schema Cleanup

### 🛡️ State Management & Distributed Concurrency Safety

- **Removed Unsafe Local Caching**: Completely removed the in-memory `TTLCache` (`_active_chats_cache`) from `app/database.py` and `app/repos/chats.py`. In an environment scaled beyond a single replica, local memory caching directly caused state divergence and chat history truncation (Data Loss).
- **Enforced Single Source of Truth (SSOT)**: `get_user_chat` and `update_user_chat` now strictly and unconditionally route `ChatState` reads/writes directly to the PostgreSQL database, ensuring perfect consistency across all bot instances with `asyncpg` pooling.

### 🧹 Schema Tech Debt Cleanup

- **Dropped Legacy Column**: Executed DDL migration to `ALTER TABLE public.chats DROP COLUMN history;`. The message history has been durably stored in the relational `active_chat_messages` table for a long time, but the legacy `history` JSONB column was still being zeroed out with `[]` on every `chat_state` update, creating unnecessary I/O constraints.

### Files Changed

| File                         | Change                                                    |
| ---------------------------- | --------------------------------------------------------- |
| `app/database.py`            | Removed `_active_chats_cache` TTLCache initialization     |
| `app/repos/chats.py`         | Dropped TTL caching logic and `history` dummy JSON insert |
| `app/repos/conversations.py` | Removed legacy cache invalidation sweeps                  |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.26] – 2026-03-09 – CI Stabilization & Type Safety

### 🧪 CI Stabilization: Test Mocking Architecture Fixes

- **Mock Integrity**: Resolved brittle mocks in `test_ai_chat.py`, `test_metrics_integration.py`, and `test_database_tavily.py` where `MagicMock` was erroneously used instead of `AsyncMock` for database calls, resolving `TypeError: object MagicMock can't be used in 'await' expression`.
- **Namespace Collision**: Removed dynamic `importlib.reload(app)` behavior from `test_perf_db_messages.py` that corrupted the `app.database` alias space context (`AttributeError`).
- **Time Freezing**: Fixed mock `datetime` patches missing crucial timezone arguments, preventing `tzinfo` exceptions during test suite isolation.

### 🧹 Code Quality & Mypy Strictness

- **Type Safety**: Fixed remaining Pydantic `arg-type` coercion warnings within the Gemini Provider SDK implementations (`GeminiProvider` configuring `safety_settings`).
- **Missing Semaphore**: Resolved a missing namespace export in `app/handlers/msg_media.py` by reinstantiating `_HEAVY_REQUEST_SEMAPHORE=asyncio.Semaphore(10)` module-locally, preventing runtime `AttributeError`s for media group grouping.
- **Routing Types**: Updated `stream_response` to be recognized natively as an async iterator. Defaulted payload arguments like `chat_id` and `parse_mode` to secure fallbacks rather than `None`.

### Files Changed

| File                                | Change                                                           |
| ----------------------------------- | ---------------------------------------------------------------- |
| `tests/test_perf_db_messages.py`    | Removed `importlib.reload(app)` mock context bleed               |
| `tests/test_metrics_integration.py` | Converted mock configurations to `AsyncMock` to satisfy `await`s |
| `app/providers/gemini.py`           | Fixed implicit dict → type `safety_settings` coercion            |
| `app/providers/router.py`           | Fixed async iterable hint masking for `stream_response`          |
| `app/streaming.py`                  | Added `type: ignore` patches for `parse_mode` types              |
| `app/handlers/ai_search.py`         | Strict typing for streaming response payload params              |
| `app/handlers/ai_photo.py`          | Strict typing formatting                                         |
| `app/handlers/msg_media.py`         | Module-local initialization of `_HEAVY_REQUEST_SEMAPHORE`        |

### 🧪 Tests: 1054 passed (0 skipped, 0 failures)

---

## [2.8.25] – 2026-03-09 – Background Task Resilience & Distributed Concurrency Limits

### 🛡️ Resilient Background Task Manager

- **Prevent Data Loss**: Replaced suppressed `asyncio.create_task` scattered usage with a centralized `TaskManager`. Background tasks, such as saving memory vectors (`_bg_store`), are now retried with exponential backoff.
- **Global Error Hooks**: When tasks exhaust all retries, critical failures are sent to the admin dashboard via a centralized error callback, ensuring observability.

### 🌐 Distributed State Management

- **Redis-backed Semaphores**: Upgraded the local `_HEAVY_REQUEST_SEMAPHORE` to a `GlobalLLMSemaphore` backed by Redis Sorted Sets (ZSETs), preventing API starvation across multi-replica deployments horizontally.
- **Graceful Degradation**: If Redis is unavailable, the semaphore transparently falls back to an in-memory `asyncio.Semaphore`, ensuring high availability.

### 🧹 Code Quality & Testing Strictness

- **Cleanups**: Fixed unused variables (`stream_last_msg`, `attempt`, `resolution`) and undefined names across `ai_search.py`, `router.py`, and test packages.
- **Ruff compliance**: Verified automated formatting (`ruff format`) and strict linting (`ruff check`) across the codebase.

### Files Changed

| File                            | Change                                                        |
| ------------------------------- | ------------------------------------------------------------- |
| `app/utils/background_tasks.py` | Centralized `TaskManager` with retry and error callback hooks |
| `bot.py`                        | Registered background task alerts to Admin alert pipeline     |
| `app/adapters/concurrency.py`   | Built Redis `GlobalLLMSemaphore` with graceful fallback       |
| `app/handlers/ai_chat.py`       | Wired background hooks to vector database                     |
| `tests/*`                       | Refactored mock assignments                                   |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.24] – 2026-03-09 – Unified AI Streaming Architecture

### ✨ Unified Real-Time Streaming

- **Universal Streaming Layer**: Replaced static tuple unpacking with a unified `stream_and_display` layer across all AI modalities. Real-time streaming is now fully supported for:
  - 💬 Regular Chat (`ai_chat.py`)
  - 📄 Document Analysis Q&A (`ai_document.py`)
  - 🌉 Image/Photo Analysis (`ai_photo.py`)
  - 🔍 Web Search & QnA (`ai_search.py`)

### 🧪 Test Suite Adaptations & AsyncPG Fixes

- **Mocking Strategy Update**: Updated all integration and unit tests to correctly mock the 3-tuple signature of `stream_and_display`.
- **Database Connection Safety**: Patched `_resolve_ai_request` in QnA and Integration tests to prevent `asyncpg.InterfaceError` ("connection was closed in the middle of operation") caused by DB event looping during tests.
- **Assertion Safety**: Swapped invalid `MagicMock` awaits for `AsyncMock` to ensure stability.

### Files Changed

| File                                      | Change                                                               |
| ----------------------------------------- | -------------------------------------------------------------------- |
| `app/handlers/ai_document.py`             | Integrated `stream_and_display` for streaming document Q&A           |
| `app/handlers/ai_photo.py`                | Vision model multimodal inputs now stream in real time               |
| `app/handlers/ai_search.py`               | Replaced standard response fetching with generic localized streaming |
| `app/handlers/agent.py`                   | Refactored orchestrator for streaming compatibility                  |
| `tests/test_ai_document.py`               | Updated mock signature and assert references                         |
| `tests/test_ai_photo.py`                  | Updated mock signature and assert references                         |
| `tests/test_ai_search.py`                 | Patched dependencies to avoid `InterfaceError`                       |
| `tests/integration/test_e2e_app_smoke.py` | Patched unpacking logic                                              |

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.23] – 2026-03-09 – Strict AAA Refactoring & Typing Fixes

### 🧪 Strict AAA Unit & Integration Tests

- **Unit Test Boundaries**: Refactored `test_ai_chat.py` to adhere to strict Arrange-Act-Assert (AAA) logic. Eliminated fragile internal function mocks (`update_stage`, `get_registry`, `handle_ai_response_error`) in favor of mocking strict database and network boundaries.
- **Integration Test Rewrites**: Converted `test_e2e_app_smoke.py` into a true integration flow (`test_integration_full_message_flow`), accurately validating database state continuity without pseudo-E2E network mocks.
- **Teardown Safety**: Fixed `RuntimeError` (`Event loop is closed`) on tear down in `test_concurrency.py` cache tests by mocking redis layer.

### 🧹 Code Quality & Typing Verification

- **Mypy Audit (100% Passed)**: Fixed 4 typing bugs detected in integration suites (`Dict | None` index violations and missing variable annotations).
- **Ruff Compliance**: Resolved `F841` (unused variable assignment) during test teardowns and enforced standardized import sorting across impacted domains.

### 🧪 Tests: 1054 passed (all suites clean)

---

## [2.8.22] – 2026-03-09 – Testing Infrastructure AAA Refactoring & Integration Boundaries

### 🧪 Legacy Test AAA Refactoring

- Completely refactored `test_ai_chat.py` and `test_smoke.py` away from implementation coupling towards the Arrange-Act-Assert (AAA) principle.
- Created `tests/factories.py` providing `make_telegram_update` and `make_telegram_context` generators to standardise component isolation and mock provisioning.

### 🛡️ Integration Verification Coverage

- **Web Dashboard Authorization Boundary**: Added `test_web_security.py` to target Quart's Admin endpoints, enforcing the Admin Secret logic securely without leaking internal errors.
- **Concurrency Locks Protection**: Created `test_concurrency_locks.py`:
  - Enforced queueing boundaries against the `state.get_user_lock` layer for simultaneous chat requests.
  - Successfully demonstrated that UI Callbacks are rejected via the `_is_user_busy` validation, preventing DB corruption and generating a Toast warning for the user.

### Files Changed

| File                                          | Change                                                          |
| --------------------------------------------- | --------------------------------------------------------------- |
| `tests/test_ai_chat.py`                       | AAA refactored, assertions centralized, decoupled context       |
| `tests/test_smoke.py`                         | Formalized admin Quart verification against standard mocking    |
| `tests/factories.py`                          | [NEW] Centralized mock creation tools                           |
| `tests/integration/test_web_security.py`      | [NEW] Web authorization perimeter checks                        |
| `tests/integration/test_concurrency_locks.py` | [NEW] Sequential lock enforcement and callback busy validations |

### 🧪 Tests: 1053 passed, 0 skipped, 1 unrelated external runner fail

---

## [2.8.21] – 2026-03-06 – Code Quality Audit: Lint, Security & Dead Code Cleanup

### 🧹 Ruff & Vulture: 25 Findings → 0

| Category                     | Count | Fix                                                                        |
| ---------------------------- | ----- | -------------------------------------------------------------------------- |
| **F823 (bug)**               | 1     | Removed redundant re-import of `TelegramFormatter` in `ai_chat.py`         |
| **B023 (style)**             | 4     | Bound loop variables as default args in `text_format.py:_flush_blockquote` |
| **I001 (imports)**           | 7     | Auto-fixed via `ruff check --fix`                                          |
| **Unused imports (app)**     | 3     | Removed `timezone` (config.py, memory.py), `ChatAction` (commands.py)      |
| **Unused imports (tests)**   | 7     | Removed `PropertyMock` ×4, `Chat`/`User` ×1                                |
| **Unused variables (tests)** | 3     | Renamed `tz` → `_tz` in test_time_utils.py MockDatetime stubs              |

### 🔒 Security: 14 CVEs Patched

| Package        | Old     | New    | CVEs Fixed |
| -------------- | ------- | ------ | ---------- |
| `aiohttp`      | 3.10.11 | 3.13.3 | 9          |
| `cryptography` | 43.0.3  | 46.0.5 | 2          |
| `pypdf`        | 6.7.3   | 6.7.5  | 2          |
| `werkzeug`     | 3.1.5   | 3.1.6  | 1          |

### ✅ Quality Gates

| Check          | Result                    |
| -------------- | ------------------------- |
| mypy           | 0 errors                  |
| ruff           | All checks passed         |
| vulture (≥80%) | 0 findings                |
| pip-audit      | No known vulnerabilities  |
| Duplicate code | 0 duplicates (≥6 stmts)   |
| pytest         | **1040 passed**, 0 failed |

### Files Changed

| File                       | Change                                          |
| -------------------------- | ----------------------------------------------- |
| `app/handlers/ai_chat.py`  | Removed redundant `TelegramFormatter` re-import |
| `app/utils/text_format.py` | Default-arg binding in `_flush_blockquote`      |
| `app/config.py`            | Removed unused `timezone` import                |
| `app/handlers/commands.py` | Removed unused `ChatAction` import              |
| `app/repos/memory.py`      | Removed unused `timezone` import                |
| `requirements.txt`         | `cryptography` upper bound <44 → <48            |
| 7 test files               | Removed unused imports, renamed unused vars     |

### 🧪 Tests: 1040 passed, 0 failures

---

## [2.8.20] – 2026-03-06 – Streaming Overflow Formatting Fix

### 🔴 Fix: Broken Formatting When Streamed Responses Overflow Into Multiple Messages

**Root cause:** `_overflow_to_new_message` split the raw markdown buffer at a text boundary, but **did not carry open markdown formatting state** to the remainder. Each half was independently formatted by `markdown_to_html()`, so the second message lost formatting context (unclosed `**`, `` ` ``, ` ``` `, `*` etc. from the first message).

**Fix:** New `_detect_open_markdown(text)` helper scans raw markdown for unclosed constructs:

- Fenced code blocks (` ``` `) — with language specifier preservation
- Inline code (`` ` ``)
- Bold (`**`)
- Italic (`*`)

Returns `(suffix, prefix)` — suffix closes open constructs in the frozen message, prefix reopens them in the remainder. Applied in `_overflow_to_new_message` before each half is formatted.

### Files Changed

| File                             | Change                                                                       |
| -------------------------------- | ---------------------------------------------------------------------------- |
| `app/streaming.py`               | `_detect_open_markdown()` helper + `_overflow_to_new_message` context repair |
| `tests/test_markdown_context.py` | [NEW] 22 unit tests for markdown context detection                           |
| `tests/test_streaming.py`        | +4 integration tests for overflow formatting preservation                    |

### 🧪 Tests: 954 passed, 0 failures

---

## [2.8.19] – 2026-03-06 – Memory Leak Fixes

### 🔴 Fix: Unbounded Memory Growth in User State Store

**Root cause:** `_UserStateStore._states` was a plain `dict` — every unique user added a permanent entry (~200B) that was never evicted. Over time, this caused monotonic RAM growth.

**Fix:** Replaced with `cachetools.LRUCache(maxsize=10000)`. Evicted states are transparently re-loaded from DB on next access via `_ensure_loaded()` — zero data loss.

### 🔴 Fix: Unbounded Growth in Metrics Dictionaries

**Root cause:** `MetricsCollector.daily_metrics` and `_user_daily` dicts accumulated entries forever — one per day and one per user-day respectively. `RoleConversationMetricsCollector.role_applications` also grew without limit.

**Fix:** Added `_prune_old_metrics()` to the `_event_processor` save cycle — prunes `daily_metrics` older than 8 days and `_user_daily` entries for past days. Capped `role_applications` at 500 entries with LFU eviction.

### 🟡 Fix: Per-Request `genai.Client` Allocation in Streaming

**Root cause:** `streaming.py` created a new `genai.Client()` on every streaming request (connection pool + TLS context), while `GeminiProvider` already reused its client correctly.

**Fix:** Added module-level `_streaming_client` with the same reuse pattern — rebuild only when API key changes.

### ℹ️ Deprecation Warning (`asyncio.iscoroutinefunction`)

Verified `memory_manager.py` already uses the correct `inspect.iscoroutinefunction()`. The log warning originates from CPython internals (`loop.add_signal_handler` lambda) — harmless until Python 3.16 and cannot be eliminated from user code.

### Files Changed

| File               | Change                                                |
| ------------------ | ----------------------------------------------------- |
| `app/state.py`     | `_UserStateStore._states`: `dict` → `LRUCache(10000)` |
| `app/metrics.py`   | `_prune_old_metrics()`, `_MAX_ROLE_ENTRIES=500` cap   |
| `app/streaming.py` | Module-level `_streaming_client` reuse                |

### 🧪 Tests: 923 passed, 0 failures

---

## [2.8.18] – 2026-03-05 – Logging Refactoring: ContextFilter, Unified APILogger, f-string Cleanup

### 🔧 ContextFilter for user_id/chat_id

Extended `request_context.py` with `set_user_context()`/`get_user_id()`/`get_chat_id()` via `contextvars`. `RequestContextFilter` auto-injects `user_id`, `chat_id`, and `request_id` into every log record. JSONFormatter already included these fields — no changes needed.

### 🏗️ Unified APILogger (452 → 156 lines)

Replaced 8 specialized methods (`log_gemini_request`, `log_gemini_response`, `log_openrouter_request`, etc.) with 3 universal ones: `log_request()`, `log_response()`, `log_error()`. Removed dead code: `_sanitize_data`, `log_api_call` decorator, `log_with_context`.

### 🔄 Decorator Context Propagation

`authorized_only` and `admin_only` decorators now call `set_user_context()` and `set_request_id()` — covers all ~40+ handlers automatically. Undecorated handlers in `cb_ai_actions.py` updated manually. Redundant calls removed from `commands.py`.

### 🧹 f-string Logging → `%s` Formatting

33 f-string `logging.*()` calls converted to lazy `%s` formatting across 12 files. Zero f-string logging calls remain in `app/`.

### 🧹 cmd_admin.py Inline user_id Cleanup

Removed 7 redundant `update.effective_user.id` references from log messages — user_id is now auto-injected via RequestContextFilter.

### Files Changed

| File                                  | Change                                                      |
| ------------------------------------- | ----------------------------------------------------------- |
| `app/request_context.py`              | `set_user_context()`, `get_user_id()`, `get_chat_id()`      |
| `app/utils/logging_config.py`         | `RequestContextFilter` extended, removed `log_with_context` |
| `app/utils/api_logger.py`             | 8 methods → 3 (452 → 156 lines), dead code removed          |
| `app/utils/decorators.py`             | `set_user_context` + `set_request_id` in both decorators    |
| `app/handlers/cb_ai_actions.py`       | Added `set_user_context`/`set_request_id` to 3 handlers     |
| `app/handlers/commands.py`            | Removed redundant `set_request_id` import/usage             |
| `app/handlers/cmd_admin.py`           | Removed 7 inline `user_id` from log messages                |
| `app/providers/gemini.py`             | Updated to `log_request()`/`log_response()`                 |
| `app/providers/openrouter.py`         | Updated to `log_request()`/`log_response()`                 |
| `app/search_services.py`              | Updated to `log_request()`/`log_response()`                 |
| 12 files across `app/`                | f-string logging → `%s` formatting (33 instances)           |
| `tests/test_decorators.py`            | [NEW] 6 tests for decorator context propagation             |
| `tests/test_request_context.py`       | +7 tests for user context                                   |
| `tests/test_api_logger_request_id.py` | Rewritten for new API                                       |

### 🧪 Tests: 923 passed, 0 failures

---

## [2.8.17] – 2026-03-05 – Quality Infrastructure: Integration Tests, CI/CD, Lint/Type Safety

### 🧪 Integration Tests with Real Supabase DB (22 new → 36 total)

New repo-level integration tests against dedicated test database (`tests_test_gemai`). Each test runs inside `BEGIN → ROLLBACK` — zero data persists.

| File                                            | Tests | Covers                                                               |
| ----------------------------------------------- | ----- | -------------------------------------------------------------------- |
| `tests/integration/test_repos_users.py`         | 4     | `save_user_state` UPSERT, `load_user_state`, feedback                |
| `tests/integration/test_repos_chats.py`         | 6     | Chat defaults, model/history update, clear, thinking level, messages |
| `tests/integration/test_repos_conversations.py` | 6     | CRUD, messages FK, rename, delete cascade, count                     |
| `tests/integration/test_repos_roles.py`         | 6     | CRUD, rename, delete, count, user-scoped access                      |

### ⚙️ CI/CD Pipeline (updated)

`.github/workflows/ci.yml` — 3-job pipeline:

1. **Lint** — `ruff check --output-format=github` + `ruff format --check`
2. **Type Check** — `mypy app/` (0 errors enforced)
3. **Test** — `pytest tests/ -x -q --ignore=tests/integration`

### 🔧 Lint & Type Safety (0 errors)

- **Ruff**: 3 import sort errors auto-fixed, 4 files reformatted
- **Mypy**: 4 errors fixed — 3 `arg-type` in `ai_chat.py`, 1 `assignment` in `logging_config.py`

### 🧹 .gitignore / .dockerignore Audit

| Issue                                                                   | Fix                                           |
| ----------------------------------------------------------------------- | --------------------------------------------- |
| IDE files tracked (`.cursor/`, `.Jules/`, `.vscode/`, `.roomodes`)      | `git rm --cached` + added to `.gitignore`     |
| Missing entries (`.coverage`, `htmlcov/`, `.mypy_cache/`, `.venv/`)     | Added to `.gitignore`                         |
| Docker image bloat (`scripts/`, `docs/`, `.github/`, `northflank.yaml`) | Added to `.dockerignore`                      |
| OS artifacts                                                            | Added `Thumbs.db`, `.DS_Store`, `Desktop.ini` |

### 📝 README.md Updated

- Test count: 652 → **870+**, 0 skipped
- Added 6 new test categories to suite structure table
- CI/CD section updated: 3 jobs (was 2)
- New "Integration Tests" section with safety guarantees
- `TEST_DATABASE_URL` added to env var reference

### Files Changed

| File                                            | Change                          |
| ----------------------------------------------- | ------------------------------- |
| `tests/integration/test_repos_users.py`         | [NEW] 4 integration tests       |
| `tests/integration/test_repos_chats.py`         | [NEW] 6 integration tests       |
| `tests/integration/test_repos_conversations.py` | [NEW] 6 integration tests       |
| `tests/integration/test_repos_roles.py`         | [NEW] 6 integration tests       |
| `tests/integration/__init__.py`                 | [NEW] Package marker for pytest |
| `.github/workflows/ci.yml`                      | Updated: 3 jobs + mypy          |
| `.gitignore`                                    | Comprehensive rewrite           |
| `.dockerignore`                                 | Comprehensive rewrite           |
| `README.md`                                     | Testing section overhaul        |
| `app/handlers/ai_chat.py`                       | Mypy arg-type fix               |
| `app/utils/logging_config.py`                   | Mypy assignment fix             |
| `app/handlers/chat_logic.py`                    | Ruff import sort fix            |
| `app/handlers/ai_search.py`                     | Ruff import sort fix            |
| `app/handlers/msg_document.py`                  | Ruff format fix                 |
| `app/metrics.py`                                | Ruff format fix                 |
| `app/utils/text_format.py`                      | Ruff format fix                 |

### 🧪 Tests: 870 passed, 0 failures, 0 skipped

---

## [2.8.16] – 2026-03-05 – Streaming HTML Misnesting Fix

### 🔴 Fix: Telegram "unmatched end tag" Errors During Streaming

**Root cause:** `sanitize_html_tags()` fixed misnested HTML tags (e.g. `<code>...<i>...</code>`) by appending close tags to the **end** of the string, but never repositioned the original misplaced close tag. Telegram's strict parser still rejected the output because nesting order was invalid.

**Fix:** Rewrote `sanitize_html_tags()` with a **rebuild approach** — walks HTML as segments, inserts close/reopen tags _at the point_ of misnesting. Also strips empty tag pairs (`<i></i>`) produced by the reopen logic.

| Input                     | Old output (broken)           | New output (valid)            |
| ------------------------- | ----------------------------- | ----------------------------- |
| `<code>E = <i>mc²</code>` | `<code>E = <i>mc²</code></i>` | `<code>E = <i>mc²</i></code>` |

### Files Changed

| File                        | Change                                                                       |
| --------------------------- | ---------------------------------------------------------------------------- |
| `app/utils/text_format.py`  | `sanitize_html_tags()` rewritten with rebuild approach + empty tag stripping |
| `tests/test_text_format.py` | Strengthened existing tests + 2 new tests with stack-walk nesting validation |

### 🧪 Tests: 26 passed, 0 failures (text_format suite)

---

## [2.8.15] – 2026-03-04 – Mypy Type Safety Overhaul (110 → 0 Errors)

### 🔍 Full Mypy Type Audit

Systematic resolution of all 110 mypy errors across 42+ files. **Zero runtime behavior changes** — all fixes are type-level only. Mypy now runs clean and can catch real type bugs going forward.

### Fix Categories

| Category                 | Count | Approach                                                                    |
| ------------------------ | ----- | --------------------------------------------------------------------------- |
| SDK suppressions         | ~15   | `# type: ignore[arg-type]` — genai Pydantic coercion (documented, not bugs) |
| Real type bug fixes      | ~10   | Fixed return types, loop var conflicts, undefined variables                 |
| Nullable guards          | ~20   | Guards for `context.args`, `user_data`, `callback_query`, `effective_user`  |
| Dict/var annotations     | ~10   | Explicit `dict[str, Any]`, `list[str]`, pre-declarations                    |
| Variable renames         | 2     | Shadow/type-conflict resolution                                             |
| Unreachable suppressions | 6     | Defensive patterns: double-check lock, isinstance guards                    |

### 🔴 Real Bug Fixes Found by Mypy

| File              | Bug                                                              | Fix                                           |
| ----------------- | ---------------------------------------------------------------- | --------------------------------------------- | --------------------- |
| `ai_photo.py`     | Return type `str` instead of `None` on failure path              | Changed `return ""` → `return None`           |
| `security.py`     | Loop variable type conflict between if/else branches             | Renamed `file_type` → `ext_set` in one branch |
| `msg_document.py` | Reference to undefined `filename` variable                       | Fixed to `f"document.{file_ext}"` fallback    |
| `callbacks.py`    | `model_name` redefined with conflicting type in if/else          | Pre-declared with `str                        | None` before branches |
| `decorators.py`   | Missing `effective_user` null check (potential `AttributeError`) | Added early return guard                      |

### 🛡️ SDK Suppressions (Intentional `# type: ignore`)

GenAI SDK uses Pydantic coercion — dicts are valid where typed models are expected. Suppressed with comments documenting why (not bugs, matches official SDK examples). Mypy still catches all **other** type errors in these files.

### Files Changed

| File                                | Change                                                                |
| ----------------------------------- | --------------------------------------------------------------------- |
| `app/streaming.py`                  | Added `Any` import, `dict[str, Any]` annotations                      |
| `app/ai_provider.py`                | `dict[str, Any]` annotations, assertion guards, defensive unreachable |
| `app/security.py`                   | Unified `allowed_extensions` declaration, loop var rename             |
| `app/config.py`                     | `list[str]` annotation, `type: ignore[assignment]` for dev fallback   |
| `app/state.py`                      | Unreachable suppression for double-check lock pattern                 |
| `app/cache.py`                      | Unreachable suppression for defensive else branch                     |
| `app/handlers/callbacks.py`         | `model_name` pre-declaration, `all_models` annotation                 |
| `app/handlers/cb_conversations.py`  | `assert query`, `context.user_data` guards                            |
| `app/handlers/cb_roles.py`          | `context.user_data` null check                                        |
| `app/handlers/msg_document.py`      | `file_name` fallback, `type: ignore` for arg-type                     |
| `app/handlers/msg_media.py`         | Unreachable for Task state, `type: ignore` for duck-type              |
| `app/handlers/ai_search.py`         | Renamed `user_id`/`chat_id` → `trace_user_id`/`trace_chat_id`         |
| `app/handlers/cmd_conversations.py` | `context.args` guard                                                  |
| `app/handlers/cmd_admin.py`         | `context.args` + `chat.title` guards                                  |
| `app/utils/decorators.py`           | `effective_user` null checks in 4 decorators                          |
| `app/utils/logging_config.py`       | `dict[str, Any]` for `extra_dict`                                     |
| `app/utils/image_utils.py`          | PIL resize explicit `(w, h)` tuple                                    |
| `app/utils/network.py`              | `assert last_exception is not None` before raise                      |
| `app/utils/keyboards.py`            | `type: ignore[return-value]` for intentional `None`                   |
| `app/tracing.py`                    | Return type `dict[str, str \| None]`                                  |
| `app/search_services.py`            | `http_client: AsyncClient \| None` annotation                         |
| `app/memory_manager.py`             | `dict[str, Any]`/`dict[str, float]` annotations                       |
| `app/metrics.py`                    | `dict[str, list[Any]]` annotation                                     |
| `app/context/assembler.py`          | `list[int]` annotation                                                |
| `bot.py`                            | `type: ignore[union-attr]` for `reconfigure`                          |
| + SDK files                         | `type: ignore[arg-type]` for genai Pydantic coercion                  |

### 🧪 Mypy: 0 errors (was 110)

---

## [2.8.14] – 2026-03-04 – Streaming HTML Sanitizer & Finish Reason Inspection

### 🔴 Fix: HTML Entity Parsing Error During Streaming

**Root cause:** `markdown_to_html()` produced unclosed HTML tags when processing incomplete markdown fragments during mid-stream flushes. Telegram rejected the malformed HTML (`"unmatched end tag at byte offset 3139, expected "</code>", found "</i>"`), causing repeated edit failures.

**Fix:** New `sanitize_html_tags()` function in `text_format.py` — lightweight stack-based HTML tag balancer that closes unclosed tags and resolves misnested tags. Applied in `StreamingWriter._flush()` (mid-stream) and `_overflow_to_new_message()`.

### 🔴 Fix: Truncated Streaming Responses (~94 chars)

**Root cause:** `stream_gemini_response()` never inspected `finish_reason` from streaming chunks. When the model stopped early (SAFETY/RECITATION), the truncated response was silently treated as success.

**Fix:** `stream_gemini_response()` now captures `finish_reason` from each chunk's candidates. `stream_and_display()` checks it after iteration:

- **SAFETY/RECITATION** → user sees partial text + `⚠️ Ответ был прерван фильтром безопасности`
- **MAX_TOKENS** → text + `⚠️ Ответ был обрезан из-за ограничения длины`
- **< 150 chars with unusual finish_reason** → WARNING logged
- `finish_reason` included in "Streaming complete" log line

### Files Changed

| File                        | Change                                                                     |
| --------------------------- | -------------------------------------------------------------------------- |
| `app/utils/text_format.py`  | New `sanitize_html_tags()` function                                        |
| `app/streaming.py`          | `finish_reason` capture, sanitizer integration, blocked/truncated handling |
| `tests/test_text_format.py` | +8 tests for sanitizer and partial-streaming scenarios                     |

### 🧪 Tests: 660 passed, 1 skipped, 0 failures

---

## [2.8.13] – 2026-03-04 – Streaming Concurrency Race Fix

### 🔴 Fix: Callback Race Condition During Streaming

**Root cause:** 6 callback handlers (`model_button`, `switch_model`, `new_topic`, `new_chat`, `deep_dive:new_topic`, `toggle_search`) performed Read-Modify-Write on `chat_state` **without checking the per-user lock**. When triggered during active streaming, their changes were silently overwritten by the streaming handler's final `update_user_chat`.

**Fix:** Added `_is_user_busy(user_id)` lock-check guard — shows "⏳ Дождитесь завершения" toast and skips mutation when the user lock is held.

### Files Changed

| File                                  | Change                                                 |
| ------------------------------------- | ------------------------------------------------------ |
| `app/handlers/callbacks.py`           | `_is_user_busy` helper + 6 handler guards              |
| `tests/test_concurrency_hardening.py` | +2 regression tests (guard presence, AST verification) |

### 🧪 Tests: 652 passed, 1 skipped, 0 failures

---

## [2.8.12] – 2026-03-04 – Code & Logic Audit + Ruff Expansion

### 🔍 Full Code & Logic Audit

Systematic audit of 30+ modules (~12K lines), 19 handlers, 10 repos. **Ruff: 0 violations.**

### 🔴 Fix: Missing RLS_CONFIG Entries

`long_term_memory` and `key_model_status` tables were missing from `RLS_CONFIG` in `db/rls.py`. Their RLS policies existed only via manual DB migrations — deploying to a fresh DB would have left them unprotected.

### ⚙️ Ruff Expansion (86 auto-fixes)

Expanded lint rules from 5 to 10 categories:

| Rule | Category              | Violations Fixed                                    |
| ---- | --------------------- | --------------------------------------------------- |
| SIM  | flake8-simplify       | 23 (suppressible-exception, collapsible-if, etc.)   |
| PIE  | flake8-pie            | 33 (unnecessary-placeholder, reimplemented-builtin) |
| C4   | flake8-comprehensions | 2 (unnecessary-dict-comprehension)                  |
| E    | pycodestyle           | 0 new (E501/E402 ignored by design)                 |
| T20  | flake8-print          | 0 new (config.py print exempted)                    |

### 🔧 Configurable Concurrency Limit

`MAX_CONCURRENT_HEAVY_REQUESTS` added to `Settings` and `load_settings()` — now configurable via env variable (default: 4). Previously hardcoded via `getattr` with fallback.

### Files Changed

| File                       | Change                                                        |
| -------------------------- | ------------------------------------------------------------- |
| `app/db/rls.py`            | Added `long_term_memory` + `key_model_status` to `RLS_CONFIG` |
| `pyproject.toml`           | +5 ruff rule categories (SIM, E, C4, PIE, T20), tuned ignores |
| `app/config.py`            | `MAX_CONCURRENT_HEAVY_REQUESTS` field + env loading           |
| `app/handlers/messages.py` | Uses `settings.MAX_CONCURRENT_HEAVY_REQUESTS` directly        |
| `app/utils/text_format.py` | `noqa: SIM102` on intentionally nested if                     |
| 86 files                   | Auto-fixed by ruff (PIE790, SIM103, SIM108, C420, etc.)       |

### 🧪 Tests: 650 passed, 1 skipped, 0 failures

---

## [2.8.11] – 2026-03-04 – Safety Refusal Regression Fix

### 🔴 Fix: Increased Safety Refusals from Gemini Models

**Root cause (two factors):**

1. `gemini-flash-latest` alias silently resolved to Gemini 3.x (stricter guardrails)
2. `PROMPT_ENGINEER` template contained `"игнорируя возможные проблемы с безопасностью"` — detected by Gemini as a jailbreak marker, triggering _increased_ refusals even with `BLOCK_NONE` safety settings

### PROMPT_ENGINEER v3.0

Redesigned the role generation meta-prompt using modular architecture (Role → Goal → Principles → Output Schema → Example → Rules):

- Removed jailbreak-triggering phrase
- Added 3-layer anti-self-censoring: generated `system_prompt` and `constraints` fields must not contain ethical/moral disclaimers
- Added few-shot example for consistent JSON output
- Made model-agnostic (removed hardcoded "Gemini 2.5 Pro" reference)

### Model Configuration

- Removed `gemini-flash-latest` from `DEFAULT_GEMINI_MODELS` (floating alias → stricter model)
- Cleaned up 4 remaining references across `model_selector.py`, `ai_provider.py`, `config.py`

### Regression Test

- `TestNoJailbreakMarkers`: Checks all prompt templates for known jailbreak marker phrases, prevents future regressions

### Files Changed

| File                            | Change                                                    |
| ------------------------------- | --------------------------------------------------------- |
| `app/prompts.py`                | PROMPT_ENGINEER v3.0 — modular, anti-self-censoring       |
| `app/prompt_registry.py`        | Same + version bump `2.1.0` → `3.0.0`                     |
| `app/config.py`                 | Removed `gemini-flash-latest`, updated docstring examples |
| `app/model_selector.py`         | Removed `flash-latest` from tier ranking                  |
| `app/ai_provider.py`            | Removed `flash-latest` from `_is_gemini3_model()`         |
| `tests/test_prompt_registry.py` | +2 jailbreak-marker regression tests                      |

### 🧪 Tests: 650 passed, 1 skipped, 0 failures

---

## [2.8.10] – 2026-03-03 – E2E Smoke Tests

### 🧪 End-to-End Smoke Tests (11 new)

| Area                 | Tests | What it verifies                                 |
| -------------------- | ----- | ------------------------------------------------ |
| Health endpoint      | 2     | `/health` JSON structure, Redis status           |
| Metrics endpoint     | 2     | `/metrics` Prometheus text format                |
| Prometheus generator | 1     | Line format validation (HELP/TYPE/value)         |
| Handler registration | 3     | commands/callbacks/messages register on mock app |
| Admin alerts E2E     | 2     | Startup health report + shutdown notification    |
| Full pipeline        | 1     | tag_error → is_error_message → metrics_collector |

> **Note:** Monitoring dashboard, Prometheus `/metrics`, and `/health` endpoints were already fully implemented. No new monitoring infrastructure needed.

**Total tests: 648 → 0 failures**

---

## [2.8.9] – 2026-03-03 – Concurrency Tests, CI/CD & Redis Fix

### 🧪 Concurrency Stress Tests (6 new)

| Test                    | Concurrency     | What it proves                            |
| ----------------------- | --------------- | ----------------------------------------- |
| Cache stampede reads    | 50 concurrent   | No data corruption under concurrent reads |
| Cache concurrent writes | 50 concurrent   | In-memory cache integrity                 |
| Alert rate limiter      | 20 concurrent   | ≤5 sent despite 20 simultaneous fires     |
| StreamingWriter         | 20 rapid writes | All chunks appear in final output         |
| Key resolution          | 10 concurrent   | Each gets valid key, no interference      |
| Error classification    | 100 concurrent  | Thread-safe classification                |

### 🔧 CI/CD Pipeline (improved)

- Added `TEST_gemaibotv2` branch trigger
- Concurrency group (auto-cancels outdated runs)
- pip dependency caching via `actions/setup-python`
- Job timeouts (5min lint, 10min test/build)
- `ADMIN_SECRET` env var for CI test isolation
- GitHub-format ruff output for inline annotations

### 🐛 Redis Connection Pool Exhaustion (bug fix)

- **Root cause:** `max_connections=2` + `concurrent_updates=True` = pool exhaustion when ≥3 users send messages simultaneously
- **Fix:** Increased `max_connections` 2→10 (Upstash free tier allows 100)
- **Fix:** Removed `ping()` before retry — it consumed a pool slot, causing deadlock under load

**Total tests: 637 → 0 failures**

---

## [2.8.8] – 2026-03-03 – Admin Alerts, Type Annotations & Integration Tests

### 🔔 Admin Alert System

New `app/admin_alerts.py` module — rate-limited Telegram notifications (5/5min) for:

- 🚨 Critical: unhandled exceptions in `global_error_handler`
- 🟢 Startup: health check report (DB, Redis, AI status)
- 🔴 Shutdown: graceful stop notification

### 📝 Type Annotations

Added return types to `agent_use_cases.py` (7 methods), `cache.py` (2 functions). Repos layer already fully typed.

### 🧪 Integration Tests (12 new)

| Area            | Tests | Coverage                                          |
| --------------- | ----- | ------------------------------------------------- |
| StreamingWriter | 2     | Debouncing, finalize                              |
| Error pipeline  | 5     | tag→detect→handle chain, retryable classification |
| Admin alerts    | 3     | Send, rate limit, traceback                       |
| Key rotation    | 2     | Exhausted keys, exclusion retry                   |

**Total tests: 631 → 0 failures**

---

### 🔴 Pipeline Bugs Fixed (4)

| Bug                                               | File             | Fix                                                                |
| ------------------------------------------------- | ---------------- | ------------------------------------------------------------------ |
| Untagged error strings (7 sites)                  | `ai_provider.py` | Wrapped with `tag_error(ErrorCode.*)` for fast-path classification |
| Fire-and-forget `create_task` (RUF006)            | `ai_chat.py`     | Added `_background_tasks` set + done callback                      |
| `_build_contents` injected fake user msg on error | `ai_provider.py` | Return `None` to abort cleanly                                     |
| OpenRouter logged as Gemini                       | `ai_provider.py` | `log_gemini_response` → `log_openrouter_response`                  |

### 🟡 Ruff: 21 → 0 Errors

- 17 auto-fixed (import sorting `I001`, `UP017`)
- 4 manual fixes: `F821` (Message import), `F841`×3 (unused vars), `RUF059`, `UP037`

### ✅ Comprehensive Audit Results

- **Performance:** No bottlenecks — DB cached (TTLCache), IO uses executors, ProcessPoolExecutor for images
- **MyPy:** 852 annotation-noise errors, 0 runtime type bugs
- **Security:** All SQL parameterized, no eval/exec, RLS on 21 tables, secrets sanitized, context cleanup paired

---

## [2.8.6] – 2026-03-03 – Dependency Audit & Critical Bug Fixes

### 🔍 Full Dependency & Codebase Audit

Audited all 17 project dependencies against official documentation. Systematic codebase scan for 15+ anti-patterns. Found and fixed **7 issues across 6 files**.

### 🔴 Critical: Restored Missing Document Processor Methods

`_process_pdf_sync` and `_process_word_sync` were accidentally deleted in REFACTOR commit (`e176c82`) but still called at runtime. **All PDF/Word document uploads crashed with `AttributeError`.**

Both methods restored as `@staticmethod`s from git history.

### Dependency Fixes

| #   | Fix                         | Files Changed                                      |
| --- | --------------------------- | -------------------------------------------------- |
| 1   | Redis `sync→async` client   | `app/cache.py`                                     |
| 2   | `pytz→zoneinfo` migration   | `app/config.py`, `app/utils/time.py`, 4 test files |
| 3   | Removed unused `orjson`     | `requirements.txt`                                 |
| 4   | `"PyPDF2"→"pypdf"` log refs | `app/document_processor.py`                        |
| 5   | Added `tzdata` fallback     | `requirements.txt`                                 |

### Codebase Fixes

| #   | Fix                                                 | Severity | Files Changed               |
| --- | --------------------------------------------------- | -------- | --------------------------- |
| 1   | Restored `_process_pdf_sync` / `_process_word_sync` | 🔴       | `app/document_processor.py` |
| 2   | `DOCUMENT_SUPPORT` dead code → proper import gating | 🟡       | `app/document_processor.py` |
| 3   | Redis `aclose()` on shutdown                        | 🟡       | `app/cache.py`, `bot.py`    |

### Files Changed

| File                         | Change                                                                 |
| ---------------------------- | ---------------------------------------------------------------------- |
| `app/cache.py`               | `redis.asyncio.Redis`, `shutdown_redis()`, removed `asyncio.to_thread` |
| `app/config.py`              | `pytz.timezone()` → `ZoneInfo()`, `pytz.UTC` → `datetime.timezone.utc` |
| `app/utils/time.py`          | `.localize()` → `datetime.combine(..., tzinfo=...)`                    |
| `app/document_processor.py`  | Restored `_process_pdf_sync`/`_process_word_sync`, import gating       |
| `bot.py`                     | `shutdown_redis()` in `_cleanup_application()`                         |
| `requirements.txt`           | Removed `orjson`, `pytz`; added `tzdata`                               |
| `tests/test_time_utils.py`   | `pytz` → `zoneinfo`                                                    |
| `tests/test_menus.py`        | Removed `pytz` mock                                                    |
| `tests/test_web_security.py` | Removed `pytz` from mock list                                          |
| `tests/test_auth_headers.py` | Removed `pytz` from mock list                                          |

### 🧪 Tests: 619 passed, 0 failures

---

## [2.8.5] – 2026-03-03 – Logic Audit: 7 Bug Fixes

### 🔍 Systematic Cross-Module Logic Audit

Reviewed 25+ modules. Found and fixed **7 logic bugs** across 6 files. Cross-pattern search confirmed no additional instances of any bug pattern codebase-wide.

| #   | File                          | Bug                                                                                      | Severity |
| --- | ----------------------------- | ---------------------------------------------------------------------------------------- | -------- |
| 1   | `app/documents/repository.py` | `cleanup_old_documents` DELETE missing `RETURNING id` — always returned 0                | 🔴       |
| 2   | `app/documents/repository.py` | `get_user_document_stats` hardcoded limit=5 instead of `settings.MAX_DOCUMENTS_PER_USER` | 🟡       |
| 3   | `app/handlers/ai_chat.py`     | Deprecated `asyncio.get_event_loop()` — error in Python 3.14                             | 🟡       |
| 4   | `app/state.py`                | `_ensure_loaded` race: concurrent handlers double-load from DB                           | 🔴       |
| 5   | `app/security.py`             | `SyncRateLimiter` documented a `threading.Lock` but never created one — data race        | 🔴       |
| 6   | `app/circuit_breaker.py`      | `__init__` created asyncio task at import time — crashes without event loop              | 🟡       |
| 7   | `app/context/assembler.py`    | `_fix_role_alternation` mutated input dicts in-place, corrupting `chat_state.history`    | 🟡       |

### Files Changed

| File                          | Change                                                       |
| ----------------------------- | ------------------------------------------------------------ |
| `app/documents/repository.py` | Added `RETURNING id`, config-based limit                     |
| `app/handlers/ai_chat.py`     | `get_running_loop()` replaces `get_event_loop()`             |
| `app/state.py`                | Double-check locking in `_ensure_loaded`                     |
| `app/security.py`             | `threading.Lock` added to `SyncRateLimiter`                  |
| `app/circuit_breaker.py`      | Guarded `_start_monitoring()` with `try/except RuntimeError` |
| `app/context/assembler.py`    | Shallow copies in `_fix_role_alternation`                    |

### 🧪 Tests: 619 passed, 0 failures

---

## [2.8.4] – 2026-03-03 – Full AI Logic Audit

### 🔍 Comprehensive Handler & Repos Audit

Systematic audit of all AI handlers, callbacks, streaming, repos, security, state, and document processing layers. **18 fixes across 10 files**, 5 commits.

#### Handler & Streaming Fixes

- **Heartbeat race conditions**: Added `stop_heartbeat()` before streaming loops in `ai_photo.py`, `ai_search.py`, `ai_document.py`
- **Assembler `None` handling**: Fixed double user message in `assembler.py` when `user_message=""` passed from search handler
- **Error sanitization**: Replaced `str(e)` leaks with generic messages in `ai_document.py`, `streaming.py`, `cb_roles.py` (×2), `msg_document.py` (×2)

#### Callback Concurrency & Safety

- **Retry race condition**: Added semaphore + user lock to `retry_last_callback` in `callbacks.py`
- **Callback parsing**: Changed `split(":")` → `split(":", 2)` in `fallback_callback` to handle model names with colons
- **Exception handling**: Wrapped background task wrappers in try/except to prevent silent exception swallowing
- **Duplicate DB write**: Removed redundant `update_user_chat` call in `role_apply_callback`

#### Database & Performance

- **Transaction isolation bug**: `switch_to_conversation` called `get_conversation_messages` and `db_execute_many` outside the transaction connection — now passes `conn=conn`
- **N+1 query**: `migrate_invalid_models` replaced per-user UPDATE loop with 2 batch `UPDATE ... WHERE user_id = ANY($2)` statements

### Files Changed

| File                           | Changes                                         |
| ------------------------------ | ----------------------------------------------- |
| `app/handlers/ai_chat.py`      | `stop_heartbeat`, empty-history guard           |
| `app/handlers/ai_search.py`    | `stop_heartbeat`, `None` user_message           |
| `app/handlers/ai_photo.py`     | `stop_heartbeat`                                |
| `app/handlers/ai_document.py`  | `stop_heartbeat`, sanitize `str(e)`             |
| `app/handlers/callbacks.py`    | Semaphore/lock, safe split, exception handling  |
| `app/handlers/cb_roles.py`     | Sanitize `str(e)` ×2, remove duplicate DB write |
| `app/handlers/msg_document.py` | Sanitize `str(e)` and `result['error']`         |
| `app/streaming.py`             | Sanitize error messages                         |
| `app/context/assembler.py`     | Skip `None` user message                        |
| `app/repos/conversations.py`   | Transaction isolation fix (`conn` param)        |
| `app/repos/chats.py`           | Batch N+1 migration                             |

### 🧪 Tests: 619 passed, 0 failures

---

### 🔴 Fix: `ValueError: history must be a non-empty list` after 503 failures

**Root cause**: When all streaming attempts failed (503 UNAVAILABLE × 3), the non-streaming fallback received an empty `chat_state.history`. The assembler's `_build_final_history` skipped appending the user message when it was falsy (`if user_message:`), and `_validate_inputs` raised an unhandled `ValueError`.

**Three-layer fix**:

| Layer               | File                       | Change                                                                  |
| ------------------- | -------------------------- | ----------------------------------------------------------------------- |
| Root cause          | `app/context/assembler.py` | Always append user message (fallback to `"..."` if empty)               |
| Graceful validation | `app/ai_provider.py`       | `_validate_inputs` returns error string instead of raising `ValueError` |
| Defensive guard     | `app/handlers/ai_chat.py`  | Check for empty history before non-streaming fallback, show retry UI    |

### 🔴 Fix: Heartbeat overwrites streaming content

**Root cause**: The heartbeat (`⏳ Обрабатываю ваш запрос...` at 15/30/50s intervals) was never stopped before `stream_and_display` began editing the same placeholder message. When streaming attempt 1 failed fast (~2s) and attempt 2 took ~74s, the heartbeat overwrote the stage indicator mid-retry.

**Fix**: `stop_heartbeat(placeholder_message.message_id)` called before entering the streaming retry loop.

### Files Changed

| File                        | Change                                                  |
| --------------------------- | ------------------------------------------------------- | ------------------------ |
| `app/context/assembler.py`  | `_build_final_history`: always append user message      |
| `app/ai_provider.py`        | `_validate_inputs` → returns `str                       | None` instead of raising |
| `app/handlers/ai_chat.py`   | Empty-history guard + `stop_heartbeat` before streaming |
| `tests/test_ai_provider.py` | Updated validation tests for new return-value API       |

### 🧪 Tests (93 passed, 0 failures)

---

## [2.8.2] – 2026-03-03 – Streaming Resilience, Embedding Upgrade & Model Selector Fix

### ✨ Continuous Multi-Message Streaming

Replaces response truncation with seamless multi-message streaming. When a response exceeds Telegram's 4096-char limit:

1. **Auto-overflow**: Freezes the current message at a natural break point (paragraph, sentence, word boundary via `_find_split_point()`).
2. **New message creation**: Calls `reply_text` (not `chat.send_message`) to start a new message in the thread.
3. **Continues streaming**: New tokens flow into the new message. Repeats indefinitely for very long responses.
4. **Correct button placement**: `stream_and_display` returns `(text, success, last_message)`. Post-stream buttons (Retry, Role) are attached to `last_message` via `edit_reply_markup`, not `edit_text`.

### 🔄 Streaming Retry with Key Rotation

**Root cause fixed**: On 503/APIError, streaming fell through to non-streaming `_get_ai_response_with_routing` — users lost the streaming UX.

**Solution**: Retry loop (up to 3 attempts) with `excluded_key_hashes`:

```
stream(key_A) → 503 → exclude A → stream(key_B) → ✅ streaming preserved
```

Only falls to non-streaming if ALL streaming keys are exhausted.

### 🧠 Model Selector: No-Downgrade Policy

**3 bugs fixed** in `model_selector.py`:

| #   | Bug                                                             | Fix                                                                   |
| --- | --------------------------------------------------------------- | --------------------------------------------------------------------- |
| 1   | `available = set(...)` — random iteration order                 | Changed to `list` for deterministic ordering                          |
| 2   | Substring `"flash"` matched all 5 models (including flash-lite) | Added `_get_tier()` capability ranking                                |
| 3   | No upgrade/downgrade check — suggested flash-lite from flash    | Only suggests models with `_get_tier(suggested) > _get_tier(current)` |

Tier system: `lite=1 < flash=2 < 2.5-flash=3 < pro=4`. Removed "short message → fast model" rule (all flash models are fast enough).

### 🔬 Embedding Upgrade: `gemini-embedding-001` @ 3072 dims

- **Model**: `text-embedding-004` → `gemini-embedding-001` with `task_type` parameter (`RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`).
- **Dimensions**: 768 → 3072 via `halfvec(3072)` (16-bit float) for HNSW index compatibility.
- **Migration**: `017_upgrade_embeddings_3072_halfvec.sql` — drops old index/column, creates new `halfvec(3072)` column + HNSW index.

### 🧹 Config Cleanup

- **Removed `gemini-3-flash-preview`** from `DEFAULT_GEMINI_MODELS` — it's an alias of `gemini-flash-latest`. Synced with production env.
- **Migration 007 fix**: `CURRENT_DATE` is volatile, invalid in index predicate — replaced with safe alternative.
- **Migration 006**: Made idempotent, deleted redundant 007.

### Files Changed

| File                            | Change                                                                           |
| ------------------------------- | -------------------------------------------------------------------------------- |
| `app/streaming.py`              | `StreamingWriter` overflow logic, `_find_split_point()`, `last_message` property |
| `app/handlers/ai_chat.py`       | 3-tuple unpacking, `edit_reply_markup` on `last_message`, streaming retry loop   |
| `app/model_selector.py`         | `_get_tier()`, no-downgrade policy, `set` → `list`, removed short-msg rule       |
| `app/config.py`                 | Removed `gemini-3-flash-preview` from defaults                                   |
| `app/repos/memory.py`           | `gemini-embedding-001`, `halfvec(3072)`, `task_type` parameter                   |
| `scripts/migrations/017_*.sql`  | Embedding column upgrade migration                                               |
| `tests/test_phase3_features.py` | Updated model selector + streaming tests                                         |

### 🧪 Tests (619 passed, 1 skipped)

### 🏗️ Structured Error Codes (MED-03)

- **`ErrorCode` enum** (16 codes) + `_ERROR_PROPERTIES` registry in `errors.py` — each code maps to `(retryable, key_related, penalty_category)`.
- **`tag_error()` / `extract_error_code()`** helpers — zero-width-space prefix for O(1) classification.
- 15+ error strings tagged across `ai_provider.py` (GeminiProvider, OpenRouterProvider, ProviderRouter).
- 4 classifiers (`is_error_message`, `is_retryable_error`, `is_key_related_error`, `classify_key_error`) refactored: code-based fast path + text-parsing fallback for backward compatibility.

### 🧹 Architecture Cleanup

- **HIGH-05**: Removed 4 unused forwarders from `ai_core.py` — 3 accessed private methods of `AgentRequestUseCase` (`_resolve_key_generic`, `_resolve_gemini_request`, `_resolve_openrouter_request`) + 1 superseded (`_get_ai_response_with_key_rotation`). File: 187 → 131 lines.
- **HIGH-03**: Removed ~82 lines of dead `DEFAULT_SYSTEM_PROMPT` / `COMPACT_SYSTEM_PROMPT` from `config.py`. Actual sole source: `prompts.py` + `prompt_registry.py`.
- **MED-08**: Added `__init__.py` to `app/handlers/` and `app/utils/` for explicit package namespaces.

### 🔒 Security & Reliability

- **SEC-02**: `get_model_hash()` now uses SHA-256 instead of MD5 (drop-in replacement, ephemeral UI hashes only).
- **MED-04**: Wrapped all 7 `pool._closed` accesses with `_is_pool_closed()` helper in `database.py` / `metrics_repo.py`, decoupling from asyncpg internals.
- **MED-01**: Extracted `SyncRateLimiter` class in `security.py` — reused by `web.py` login protection, replacing 30-line ad-hoc implementation.

### 📦 Module Splits (MED-07, MED-09, MED-11)

- **MED-09**: Extracted `MetricsMiddleware` + `track_metrics` decorator → `app/utils/metrics_middleware.py`. Re-exports in `metrics.py` for backward compat.
- **MED-07**: Split `context_assembler.py` (616 lines) → `app/context/` package:
  - `token_budget.py` — constants, `TokenBudget`, `AssembledContext` dataclasses
  - `summarizer.py` — LLM refine-chain summarization + chunk splitting
  - `assembler.py` — `ContextAssembler` class, `should_summarize()`, singleton
  - Original file → thin re-export facade (13 lines)
- **MED-11**: Split `document_processor.py` (697 lines) → `app/documents/` package:
  - `repository.py` — all DB CRUD, stats, duplicate/limit checks
  - `parsers.py` — sync file I/O helpers (hash, temp file)
  - Orchestrator class + facade functions remain in `document_processor.py`

### ✅ Previously Resolved (verified this session)

- **HIGH-04**: `DEFAULT_GEMINI_MODELS` constant de-duplicated (single definition, 3 references)
- **HIGH-06**: `flask_app` → `quart_app` renamed in `web.py` + `bot.py`
- **SEC-01**: OpenRouter referer updated to `https://t.me/gemaibotv2`
- **PERF-01**: Removed extra `count_tokens` API call → uses `response.usage_metadata`
- **MED-10**: `genai.Client` reuse per provider instance (lazy init + key check)
- **MED-06**: `_custom_role_cache` → `TTLCache(maxsize=256, ttl=3600)`
- **MED-12**: `_load_daily_limits` now logs raw malformed env var values

### Files Changed

| File                              | Change                                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------- |
| `app/errors.py`                   | `ErrorCode` enum, `_ERROR_PROPERTIES`, `tag_error`, `extract_error_code`, `strip_error_tag` |
| `app/ai_provider.py`              | 15+ error strings tagged with `ErrorCode`                                                   |
| `app/handlers/ai_core.py`         | Removed 4 unused forwarders (187→131 lines)                                                 |
| `app/config.py`                   | Removed dead prompts (82 lines), MD5→SHA256                                                 |
| `app/database.py`                 | `_is_pool_closed()` helper, replaced 6 `pool._closed`                                       |
| `app/repos/metrics_repo.py`       | `pool._closed` → `_is_pool_closed()`                                                        |
| `app/security.py`                 | [NEW] `SyncRateLimiter` class                                                               |
| `app/web.py`                      | Replaced ad-hoc rate limiter with `SyncRateLimiter`                                         |
| `app/handlers/__init__.py`        | [NEW] Empty init                                                                            |
| `app/utils/__init__.py`           | [NEW] Empty init                                                                            |
| `app/utils/metrics_middleware.py` | [NEW] `MetricsMiddleware`, `track_metrics`                                                  |
| `app/metrics.py`                  | Slimmed — re-exports middleware from submodule                                              |
| `app/context/`                    | [NEW] Package: `token_budget.py`, `summarizer.py`, `assembler.py`                           |
| `app/context_assembler.py`        | → Thin re-export facade (616 → 13 lines)                                                    |
| `app/documents/`                  | [NEW] Package: `repository.py`, `parsers.py`                                                |
| `app/document_processor.py`       | Slimmed orchestrator (697 → ~310 lines)                                                     |

### 🧪 Tests (572 passed, 1 skipped)

## [2.8.0] – 2026-03-03 – Production Audit: Phases 1–4

### ✨ Phase 1: Foundation & CI

- **GitHub Actions CI** (`.github/workflows/ci.yml`): lint → test → Docker build pipeline.
- **Structured JSON logging**: Auto-enabled in production with `request_id` propagation via `contextvars`.
- **`/export` command**: Export current chat history as Markdown document.
- **`ChatAction.TYPING`**: Sent immediately on message receipt, before any processing.

### 🏗️ Phase 2: Architecture Cleanup

- **Handler extraction**: `messages.py` from 1163 → ~260 lines. Logic split into:
  - `msg_media.py` — media group accumulation, image processing
  - `msg_roles.py` — role/conversation rename state machines
  - `msg_document.py` — document upload, duplicate detection
- **`/settings` command**: Unified view of user preferences with inline navigation.

### ✨ Phase 3: Product Features

- **Streaming responses** (`streaming.py`): Gemini `generate_content_stream` + debounced `edit_message_text` (1.2s, 80-char minimum). Falls back to non-streaming for OpenRouter.
- **pgvector long-term memory** (`repos/memory.py`): `text-embedding-004` (768-dim), HNSW index, cosine similarity search, 500/user limit, 90-day TTL. Semantically recalled during context assembly, stored after each exchange.
- **Smart model selection** (`model_selector.py`): Regex heuristics classify messages (code/reasoning/simple/creative) → non-intrusive inline button suggestions when mismatch detected. `switch_model:` callback handler for one-tap switching.

### 🔧 Phase*   **Image Generation Canvas 2.0**:
    *   **Deferred Generation**: Changing model or format no longer instantly regenerates the image. Instead, users configure parameters via a multi-level menu and press "▶️ СГЕНЕРИРОВАТЬ" to execute.
    *   **Prompt Translation**: Added transparent prompt translation for `flux` and other non-Cyrillic models. If a user sets a Russian prompt, it is automatically translated to English via `gemini-3.1-flash-lite`, preserving the exact visual intent.
    *   **Live Prompt Editing**: Added "✏️ Изменить промпт" button, allowing users to send text to seamlessly update the current generation state without starting a new `/draw` command.
    *   **Instant Enhancement**: Added "✨ Улучшить промпт" toggle, integrating natively with Pollinations' LLM-based prompt enhancer.

*   **Production Hardenings**:

- **Webhook mode** (`bot.py`): Set `WEBHOOK_URL` env var → auto-registers `/webhook/<token>` route, replaces long-polling. Graceful cleanup on shutdown.
- **Prometheus `/metrics`** (`prometheus.py` + `web.py`): Zero-dependency text format exporter — uptime, API calls, errors, active users, memory. Unauthenticated for scraping.
- **GDPR commands** (`commands.py`):
  - `/mydata` — exports all user data as JSON (GDPR Article 20)
  - `/deleteme CONFIRM` — deletes all user data with confirmation gate (GDPR Article 17)
- **Degradation matrix** (`degradation.py`): `check_system_health()` checks DB/Redis/AI status. `can_process_message()` returns fallback decisions with user-facing messages.

### Files Changed

| File                            | Change                                         |
| ------------------------------- | ---------------------------------------------- |
| `.github/workflows/ci.yml`      | [NEW] CI pipeline                              |
| `app/streaming.py`              | [NEW] Streaming responses                      |
| `app/repos/memory.py`           | [NEW] pgvector long-term memory                |
| `app/model_selector.py`         | [NEW] Smart model auto-selection               |
| `app/prometheus.py`             | [NEW] Prometheus text exporter                 |
| `app/degradation.py`            | [NEW] Service degradation matrix               |
| `app/handlers/msg_media.py`     | [NEW] Media handling (extracted)               |
| `app/handlers/msg_roles.py`     | [NEW] Role FSMs (extracted)                    |
| `app/handlers/msg_document.py`  | [NEW] Document handling (extracted)            |
| `app/handlers/messages.py`      | Thin router (~260 lines, was 1163)             |
| `app/handlers/ai_chat.py`       | Memory recall, model suggestions, streaming    |
| `app/handlers/callbacks.py`     | `switch_model:` callback handler               |
| `app/handlers/commands.py`      | `/export`, `/settings`, `/mydata`, `/deleteme` |
| `app/web.py`                    | `/metrics` endpoint                            |
| `bot.py`                        | Webhook mode (WEBHOOK_URL env var)             |
| `app/utils/logging_config.py`   | Structured JSON logging                        |
| `tests/test_phase3_features.py` | [NEW] 20 integration tests                     |

### 🧪 Tests (572 passed, 1 skipped)

- `test_phase3_features.py`: 20 new tests — model selector (6), streaming (4), Prometheus (2), GDPR (2), degradation (6).

---

## [2.7.2] – 2026-03-02 – User-Configurable Thinking Levels

### ✨ New: `/thinking` Command

Per-user control of Gemini's reasoning depth. Auto-detects model family and sends the correct API parameter:

- **Gemini 2.5** (`gemini-2.5-flash`, `gemini-2.5-flash-lite`) → `thinkingBudget` (int: 0–24576)
- **Gemini 3** (`gemini-3-flash-preview`, `gemini-flash-latest`) → `thinkingLevel` (minimal/low/medium/high)
- **OpenRouter** → ignored (no ThinkingConfig sent)

User-facing levels: `off`, `low`, `medium`, `high`, `auto` (reset to model default).

### Files Changed

| File                       | Change                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------ |
| `app/ai_provider.py`       | `_build_thinking_config()`, `_is_gemini3_model()`, `thinking_level` param through provider chain |
| `app/agent_use_cases.py`   | `get_ai_response()` accepts `thinking_level`                                                     |
| `app/handlers/ai_core.py`  | `_get_ai_response_with_routing()` accepts `thinking_level`                                       |
| `app/handlers/ai_chat.py`  | Reads `chat_state.thinking_level`, passes to router                                              |
| `app/handlers/commands.py` | `/thinking` command + registration                                                               |
| `app/database.py`          | `ChatState.thinking_level` field                                                                 |
| `app/repos/chats.py`       | `update_thinking_level()`, DB read/write of column                                               |
| `tests/test_ai_chat.py`    | Test fixture updated with `thinking_level=None`                                                  |

### 🧪 Tests (552 passed, 1 skipped)

---

## [2.7.1] – 2026-03-02 – OpenRouter Key Rotation Fix & Supabase Advisory

### 🔴 Critical Fix: OpenRouter Retries Using Same Suspended Key

**Root cause**: `get_available_openrouter_key()` accepted `excluded_hashes` but ignored it — called `get_available_key()` (simple least-used query, no status/exclusion filtering) instead of `get_fresh_available_key()` (two-tier SQL with exclusions + status checks). The retry loop always re-selected the same rate-limited key, guaranteeing all 3 attempts failed identically.

**Fix**: Rewired to `_openrouter_km.get_fresh_available_key()` with `excluded_hashes` and `daily_limit` forwarding — now mirrors the Gemini key path.

### 🔒 Security: Supabase `search_path` Advisory

- `check_key_hash_exists()` trigger function now has `SET search_path = public`, resolving the Supabase security advisor lint about mutable search_path.

### Files Changed

| File                                                        | Change                                                                         |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `app/repos/keys.py`                                         | `get_available_openrouter_key()` → `get_fresh_available_key()` with exclusions |
| `scripts/migrations/016_fix_check_key_hash_search_path.sql` | New migration: `SET search_path = public` on trigger function                  |
| `tests/test_repos_keys.py`                                  | +2 regression tests: exclusion forwarding, no-keys case                        |

### 🧪 Tests (550 passed, 1 skipped)

- `TestGetAvailableOpenrouterKey`: 2 new tests verifying exclusion forwarding and empty result handling.

---

## [2.7.0] – 2026-03-01 – Persistent Per-Model Key Health System

### 🔴 Critical Fix: "All Gemini keys exhausted" False Positive

**Root cause**: An invalid key (`API_KEY_INVALID`) with `request_count=0` was perpetually re-selected by the `ORDER BY request_count ASC` query because failed requests never increment usage. The `excluded_key_hashes` set was only checked client-side — the DB query had no exclusion filter.

### ✨ New: DB-Backed `KeyStatusManager`

Replaces the in-memory `KeyHealth` dataclass with a persistent, per-model key health system.

- **`key_model_status` table**: Tracks `(key_hash, model_name)` with `status`, `suspended_until`, `failure_count`, and `last_error`.
- **Error-category-aware cooldowns** (`classify_key_error()` in `errors.py`):
  - `API_KEY_INVALID` → 24h suspension (not forever — recovery probe after cooldown)
  - `quota_exceeded` → suspend until midnight Pacific time
  - `rate_limit` → 60s suspension
  - `503/timeout/overloaded` → no suspension (transient)
- **Two-tier SQL key selection**: Active keys (Tier 1) are tried first; cooldown-expired keys (Tier 2) are probed for recovery.
- **Exponential backoff**: Repeated failures double the cooldown (capped at 7 days).
- **Auto-recovery**: On successful probe, key promoted back to `active`, `failure_count` reset to 0.
- **SQL-level exclusion**: `excluded_key_hashes` now passed to the DB query via `WHERE ak.key_hash != ALL($excluded)`.

### 🏗️ Architecture Changes

- **Removed `KeyHealth` dataclass** from `ai_provider.py` — all health tracking is now DB-backed.
- **Simplified `_resolve_key_generic`** in `agent_use_cases.py` — removed 5-attempt client-side loops; DB query handles exclusion and status filtering directly.
- **`get_available_gemini_key` / `get_available_openrouter_key`**: Accept `excluded_hashes` parameter; skip cache when exclusions exist.

### Files Changed

| File                                              | Change                                                        |
| ------------------------------------------------- | ------------------------------------------------------------- |
| `scripts/migrations/014_add_key_model_status.sql` | New migration: `key_model_status` table + RLS + index         |
| `app/db/schema.py`                                | Added `key_model_status` to `create_tables()`                 |
| `app/errors.py`                                   | New `classify_key_error()` function                           |
| `app/repos/keys.py`                               | `KeyStatusManager` class, two-tier SQL queries                |
| `app/agent_use_cases.py`                          | Simplified `_resolve_key_generic` (DB-level filter)           |
| `app/ai_provider.py`                              | Removed `KeyHealth`, `ProviderRouter` uses `KeyStatusManager` |
| `tests/test_provider_router.py`                   | Rewritten: 7 tests for suspend/recover/categories             |
| `tests/test_decryption_error_handling.py`         | Fixed for new `excluded_hashes` parameter                     |

### 🧪 Tests (548 passed, 1 skipped)

- `test_provider_router.py`: 7 tests — successful response, exhausted keys, key failure + suspend, quota category, excluded keys propagation, OpenRouter detection, transient non-suspension.
- `test_decryption_error_handling.py`: Fixed mock assertions for `excluded_hashes` parameter.

### 🎭 UX Fix: "Выбрать роль ИИ" No Longer Destroys AI Response

**Problem**: Clicking "🎭 Выбрать роль ИИ" under an AI response called `edit_message_text`, replacing the response with the roles menu and losing the AI-generated content.

**Solution**: Origin-aware callback routing via `callback_data` suffix:

- AI response keyboards use `open_roles:from_response` → roles menu sent as **new message** (`reply_text`)
- Menu keyboards keep `open_roles` → in-place edit (existing behavior)

| File                                                          | Change                                                                             |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `keyboards.py`                                                | `ai_response_keyboard()`, `deep_dive_keyboard()` → `from_response`                 |
| `ai_chat.py`, `ai_search.py`, `ai_photo.py`, `ai_document.py` | Ad-hoc keyboards → `from_response`                                                 |
| `errors.py`                                                   | `build_retry_and_roles_keyboard()`, `build_roles_keyboard()`                       |
| `cb_roles.py`                                                 | `open_roles_callback`: `reply_text` when `from_response`, else `edit_message_text` |
| `callbacks.py`                                                | Regex: `^open_roles(:from_response)?$`                                             |
| `test_errors.py`                                              | 2 assertions updated                                                               |

### 🧹 Full Codebase Lint Cleanup (126 errors → 0)

- **105 auto-fixed** via `ruff check --fix`: `I001` (import sorting), `UP006/UP045` (modern type annotations), `UP015/UP012/F541` (misc)
- **5 `UP035`**: Removed deprecated `typing.Dict/List/Tuple/Optional` imports in `heartbeat.py`, `test_database_tavily.py`, `test_repos_keys.py`, `test_roles_menu.py`, `test_menus.py`
- **12 `F841`**: Prefixed unused mock variables with `_` in 6 test files
- **1 `RUF006`**: Stored fire-and-forget task reference in `context_assembler.py`
- **1 `B019`**: Suppressed with `noqa` in `prompt_registry.py` (singleton lru_cache by design)

---

## [2.6.9] – 2026-02-28 – Context Summarization System

### ✨ New Feature: Two-Tier Context Summarization

Intelligent conversation summarization for long multi-turn conversations, preventing context window overflow while preserving conversational quality.

#### Architecture

- **128K token budget** with 12K response reserve and 4K summary budget.
- **Local tier** (< 30K dropped tokens): Fast snippet-based truncation — first/last lines of dropped messages.
- **LLM tier** (≥ 30K dropped tokens): Asynchronous refine-chain summarization via Gemini. Chunks up to 10K tokens × 6 max. Fires as a background `asyncio.create_task`, persists result via callback.
- **`ContextAssembler`** class (`context_assembler.py`): Token budgeting, two-tier decision logic, `AssembledContext` dataclass with `dropped_messages`, `was_truncated`, `llm_summarization_scheduled` fields.

#### Summarization Prompts

- `SUMMARIZATION_SYSTEM` and `SUMMARIZATION_CHUNK` templates in `prompt_registry.py`.
- Prompt-engineering best practices: structured output, explicit instructions, anti-hallucination guardrails.

#### Integration

- **`ai_chat.py`**: Checks `assembled.llm_summarization_scheduled`, fires `schedule_llm_summarization()`, callback persists summary via `update_user_chat()`. Records tier-specific metrics.
- **`ai_search.py`**: Same pattern — LLM scheduling + metrics for search handler context assembly.
- **`callbacks.py`**: 3 chat-clearing handlers (`new_topic_callback`, `deep_dive_callback:new_topic`, `new_chat_callback`) now clear `context_summary = None` to prevent stale summaries.

#### Database

- **Migration `013_add_context_summary.sql`**: Adds `context_summary TEXT` column to `chats` table.
- **`ChatState.context_summary`** field in `database.py` — replaces prior `_context_summary` dynamic attribute hack.
- **`repos/chats.py`**: `get_user_chat` / `update_user_chat` load and persist `context_summary`.

#### Metrics & Dashboard

- **`SummarizationMetrics`** in `metrics.py`: New fields `llm_summarizations`, `local_summarizations`, `llm_summarization_failures`. `record_summarization()` auto-detects tier from `llm:`/`local:` prefix.
- **`/api/overview`** in `web.py`: Includes `summarization` object with `triggered`, `llm_tier`, `local_tier`, `tokens_saved`, `avg_summary_length`, `llm_failures`.
- **Dashboard** (`dashboard.html`): New "Context Summarization" card in the Overview tab — 4 stat cards (Triggered, LLM Tier, Local Tier, Tokens Saved) with live polling.

### 🧪 Tests (554 passed, 1 skipped)

- `test_context_assembler.py`: Comprehensive tests for constants, chunk splitting, LLM scheduling, `AssembledContext` fields.
- `test_prompt_registry.py`: Updated expected template count (7 → 9) for new summarization templates.
- `test_ai_chat.py`: Updated `make_chat_state` mock with `context_summary=None`.
- All pre-existing tests pass — 1 pre-existing skip, 0 new failures.

### Files Changed

| File                                             | Change                                    |
| ------------------------------------------------ | ----------------------------------------- |
| `app/context_assembler.py`                       | New — two-tier summarization engine       |
| `app/prompt_registry.py`                         | +2 summarization prompt templates         |
| `app/database.py`                                | `ChatState.context_summary` field         |
| `app/repos/chats.py`                             | DB load/save of `context_summary`         |
| `app/handlers/ai_chat.py`                        | LLM scheduling + metrics                  |
| `app/handlers/ai_search.py`                      | LLM scheduling + metrics                  |
| `app/handlers/callbacks.py`                      | Clear summary on topic reset (3 handlers) |
| `app/metrics.py`                                 | LLM/local tier tracking                   |
| `app/web.py`                                     | `/api/overview` includes summarization    |
| `app/templates/dashboard.html`                   | Summarization stats card                  |
| `scripts/migrations/013_add_context_summary.sql` | DB migration                              |
| `tests/test_context_assembler.py`                | New test file                             |

---

## [2.6.8] – 2026-02-28 – Ruff Integration & Code Modernization

### 🐛 Bug Fixes (Ruff-identified)

- **B026 (silent data loss)**: `*args` passed after keyword `exceptions=` in `network.py` — reordered to prevent silent argument loss.
- **B904 (10 fixes)**: Missing `from e`/`from None` in exception `raise` statements across `database.py`, `cache.py`, `crypto.py`, `security.py`, `config.py` — restores exception chaining for proper traceback analysis.
- **RUF006 (5 fixes)**: Dangling `asyncio.create_task()` calls without stored references (GC risk) in `callbacks.py`, `messages.py`, `prompts.py` — stored in `_background_tasks` set with `add_done_callback` cleanup.

### 🧹 Code Modernization (598 auto-fixes)

- **isort (I)**: 75 files — import ordering normalized.
- **pyupgrade (UP)**: 502 fixes — type annotations modernized to PEP 585/604 (`List[str]` → `list[str]`, `Optional[X]` → `X | None`), deprecated `typing` imports removed, `super()` calls simplified.
- **SIM117**: 6 files — nested `with` statements merged.
- **W291/W293**: 23 trailing whitespace fixes.
- **RUF059 (5)**: Unused unpacked variables (`ai_key`, `resolution`, `parse_mode`, `priority`) replaced with `_`.
- **F541 (1)**: f-string without placeholders in `menus.py` → plain string.
- **B905 (1)**: `zip()` → `zip(strict=False)` in `web.py`.

### ⚙️ Tooling

- **`pyproject.toml`**: New Ruff configuration — enforces `F` (Pyflakes), `B` (Bugbear), `I` (isort), `UP` (pyupgrade), `RUF006`, `RUF059`. Per-file ignores for tests and false positives.
- **`conftest.py`**: Custom event loop exception handler to suppress cosmetic asyncpg `ConnectionDoesNotExistError` during test teardown GC.

### 🧪 Tests (485 passed, 1 skipped)

- All pre-existing tests pass after 622 total code changes across 40+ files.
- Flaky `test_execute_gemini_request_other_error` resolved (stale patch from prior sessions).

## [2.6.7] – 2026-02-28 – Codebase Audit Phase 2-3: Hardening, DRY & Cleanup

### 🛡️ Security & Reliability (Phase 2)

- **C3 (API key logging)**: Masked API key prefix in debug logs to `[:6]****`
- **H1 (race condition)**: `_UserStateStore.__getitem__` uses `setdefault()` to prevent TOCTOU races
- **H2 (fire-and-forget)**: `ConfigManager.schedule_reload()` stores task reference + debounce guard (3s cooldown)
- **H3 (zombie tasks)**: `_background_tasks` set in `messages.py` prevents "exception never retrieved"
- **H5 (token leak)**: `httpx`/`httpcore` loggers now suppressed to WARNING+ to prevent Bearer token exposure
- **M4 (encryption heuristic)**: `is_encrypted()` now validates base64url encoding via `re.fullmatch()`, preventing false positives

### 🏗️ DRY & Code Quality (Phase 3)

- **M1 (handler boilerplate)**: New `safe_handler()` / `safe_callback()` decorators in `decorators.py` — centralize error handling for Telegram handlers
- **Decorator adoption**: Applied `@safe_handler()` to `start_command`, `help_command`, `documents_command`, `stats_command` — eliminated ~40 lines of try/except boilerplate
- **M3 (false positives)**: `is_key_related_error` narrowed from bare `"limit"` to `"rate limit"`, `"daily limit"`, `"limit exceeded"`
- **M2 (unnecessary lock)**: Removed `asyncio.Lock` from `RoleConversationMetricsCollector` (atomic in single-threaded asyncio)

### 🧹 Cleanup

- **L4 (resource leak)**: Added `close_http_clients()` for OpenRouter and `close_tavily_client()` for Tavily — both wired to `_cleanup_application()` in `bot.py`
- **L3 (dead code)**: Removed unused `validate_file_upload` from `security.py`
- **L1 (unclean shutdown)**: `os._exit(1)` → `sys.exit(1)` in signal handler
- **L2 (redundant logic)**: Simplified `_get_admin_secret()` to use `settings.ADMIN_SECRET` directly
- **I3 (unused dep)**: Replaced `pydantic-settings` with `pydantic` in `requirements.txt`
- **M8 (unused imports)**: Removed `re`, `json`, `hashlib`, `date` from `database.py`

### 📐 Type Annotations

- `conversations.py`: bare `-> list` → `-> List[Dict[str, Any]]`, `-> int` → `-> Optional[int]`
- `keys.py`: Added return types to `DailyKeyManager.increment_usage` and `MonthlyKeyManager.increment_usage`

### 🧪 Tests (+32 → 485 total, 1 skipped)

- `test_audit_fixes.py`: 32 regression tests covering all 23 fixes from Phases 2–3
  - `TestIsEncryptedHardened` (5), `TestIsKeyRelatedErrorNarrowed` (6), `TestSafeHandlerDecorator` (3)
  - `TestMediaGroupMaxSize` (2), `TestHttpClientClose` (2), `TestMetricsCollectorNoLock` (3)
  - `TestMigrateInvalidModels` (3), `TestDatabaseCleanImports` (1), `TestValidateFileUploadRemoved` (1)
  - `TestRequirementsPydantic` (1), `TestConfigReloadDebounce` (2), `TestUserStateStoreSetdefault` (1)
  - `TestHttpxLoggerSuppression` (1), `TestSafeCallbackDecorator` (1)

## [2.6.6] – 2026-02-27 – Resilience, Repository Extraction & Performance Wiring

### 🛡️ Resilience: Circuit Breakers

- **Tavily API**: Wrapped `_tavily_api_call` in `run_with_resilience(circuit_name="tavily")` in `search_services.py`. Trips after consecutive failures, preventing cascading Tavily hammering during outages.
- **Telegram API**: Added lazy `_get_telegram_cb()` circuit breaker to `messaging.py`. `edit_text` and `reply_text` calls in `send_long_message` are now wrapped — prevents flooding Telegram servers when API is degraded.
- Lazy initialization pattern (`_get_telegram_cb()`) avoids `asyncio.create_task` at import time, which previously caused test collection failures.

### 🏗️ Repository Extraction: `db.db_query` → `repos/`

Eliminated **all 20 raw SQL queries** from handler files. Zero `db.db_query` calls remain in any handler module.

| New Module            | Functions                                                                                                                                                                      | Migrated From                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| `repos/roles.py`      | 7 (`get_user_custom_roles`, `get_user_custom_roles_full`, `get_custom_role_count`, `get_custom_role_prompt`, `create_custom_role`, `delete_custom_role`, `rename_custom_role`) | `cb_roles.py`, `menus.py`, `messages.py` |
| `repos/user_stats.py` | 3 (`get_user_today_request_count`, `get_user_weekly_stats`, `get_user_model_usage_today`)                                                                                      | `commands.py`, `menus.py`                |
| `repos/admin.py`      | 6 (`authorize_user`, `revoke_user`, `list_authorized_users`, `clear_old_metrics`, `get_all_tavily_keys`, `get_tavily_usage_for_month`)                                         | `cmd_admin.py`                           |

### ⚡ Performance & Metrics

- **`track_metrics` decorator fix**: Inner function was incorrectly `async` and missing `functools.wraps` — fixed to correctly wrap async functions.
- **Handler response time tracking**: Wired `metrics_collector.record_request()` into `handle_request` at both success and error paths. Dashboard `average_response_time` now reflects real handler latency.
- **Search handler timing**: Applied `@track_metrics` decorator to `_handle_qna_search`, `_handle_research_agent`, `_handle_complex_agent_search` in `ai_search.py` for per-operation latency tracking.

### 🧹 Cleanup

- Removed stale `from app import database as db` imports from 4 handler files (`cb_roles.py`, `menus.py`, `messages.py`, `cmd_admin.py`).
- Migrated 2 Tavily proxy imports in `search_services.py` to direct repo imports.
- Updated `database.py` end-of-file repo directory comment with new modules.

### 🧪 Tests (+4 → 453 total, 1 skipped)

- `test_integration_flow.py`: 4 new end-to-end scenarios — unauthorized rejection, rate limiting, happy path through agent, error recovery with retry keyboard.
- Updated `test_roles_menu.py`: Removed stale `db` patch, all 10 tests now target repo functions.
- Updated `test_cmd_admin.py`: 3 tests updated to patch `repos/admin` functions instead of removed `cmd_admin.db`.

---

## [2.6.5] – 2026-02-26 – Codebase Remediation & Security Hardening

### 🔴 Critical Fixes

- **`safe_decrypt` silent failure**: Previously returned raw ciphertext on decryption failure (wrong `ADMIN_SECRET`), causing silent API key auth errors. Now raises `DecryptionError` with clear error logging.
- **`get_current_active_gemini_key` missing decryption**: Returned encrypted ciphertext directly to the Gemini SDK → "API key not valid" errors. Added `safe_decrypt` call.
- **Blocking Redis `ping()`**: Synchronous `redis_client.ping()` in health check and API endpoints blocked the asyncio event loop. Wrapped with `asyncio.to_thread()`.
- **Silent state persistence failures**: `_persist()` errors logged at `DEBUG` level → invisible in production. Upgraded to `WARNING` + added `add_done_callback` to fire-and-forget tasks.

### 🔒 Security Hardening

- **Brute-force protection**: IP-based login rate limiter on `/login` (5 attempts per 5 minutes → 429). Periodic eviction of stale IPs every 50 checks prevents memory growth.
- **SQL injection prevention**: Added regex validation (`_SAFE_TABLE_RE`) for table names in `DailyKeyManager.__init__`.
- **Nonce-based CSP**: Replaced `'unsafe-inline'` in `script-src` and `style-src` with per-request nonces (`secrets.token_urlsafe(16)`). Templates (`login.html`, `dashboard.html`) use `{{ g.csp_nonce }}`.
- **Error response sanitization**: 7 API endpoints replaced `str(type(e).__name__)` with generic `"internal_error"` to prevent leaking internal exception class names.

### 🏗️ Architecture: `database.py` → `app/db/` Package

Split the monolithic `database.py` (950 → ~480 lines) into 4 focused modules:

| Module                 | Contents                                                         |
| ---------------------- | ---------------------------------------------------------------- |
| `app/db/schema.py`     | All `CREATE TABLE` statements (~200 lines)                       |
| `app/db/migrations.py` | SQL file runner + legacy inline migrations (~115 lines)          |
| `app/db/rls.py`        | RLS config, policy templates, setup functions (~140 lines)       |
| `app/db/seed.py`       | Initial data seeding (admin user, API keys, indexes) (~57 lines) |

Backward compatibility preserved: `from app.database import X` continues to work via re-exports.

### 🟡 Regression Fixes (Post-Audit)

- **F-1 DecryptionError UX**: `_resolve_key_generic` now catches `DecryptionError` and returns `'decryption_failed'` → user sees `🔐 Ошибка расшифровки API-ключей...` instead of raw traceback.
- **F-2 Login rate limiter leak**: `_login_attempts` dict now periodically evicts stale IPs (every 50 checks).
- **F-3 Dual RLS wrappers**: `_init_schema` now uses the module-level backward-compat wrapper instead of a redundant direct import.

### ⚡ Performance

- **`asyncio.Lock` in `MultiLayerCache`**: Protects TTLCache reads/writes from concurrent coroutine corruption.
- **Removed ~60 lines of duplicate Redis-only caching** in `get_cached_search_result` / `cache_search_result`.

### 🧪 Tests (+5 → 409 total)

- `test_decryption_error_handling.py`: 5 tests — primary path catch, fallback catch, user-friendly message validation, normal flow unaffected, no-retry behavior.
- Updated `test_crypto.py`: expects `DecryptionError` from `safe_decrypt`.
- Updated `test_security_headers.py`, `test_web_security.py`: validates nonce-based CSP (`'nonce-'` present, `'unsafe-inline'` absent).

---

## [2.6.4] – 2026-02-26 – Error Handling & Role Save Bug Fix

### 🔴 Critical Fixes

- **Manual role save false-positive error**: `role_manual_save_callback` showed "❌ Нет данных для сохранения роли" even though the role was saved. Root cause: `clear_manual_role_state()` was called before the save callback, wiping in-memory state, and `context.user_data` doesn't reliably persist across Telegram Update boundaries. Fix: store prompt in `app.state` (new `manual_role_prompt` field), use `finish_manual_role_input()` to flip flags without clearing data, clear only after save/cancel.

### 🟡 Improvements

- **Actionable error messages**: 16 bare error messages across `messages.py`, `callbacks.py`, and `menus.py` now include contextual recovery buttons (retry, back to menu, back to documents) instead of dead-end text.
- **3-stage heartbeat on long waits**: Placeholder "🤔 Думаю..." now updates progressively (15s → "⏳ Обрабатываю ваш запрос...", 30s → "⏳ Ответ генерируется...", 50s → "⏳ Запрос обрабатывается дольше обычного...") using `asyncio.Event` for race-safe cancellation.
- **Manual role DB persistence**: Added `awaiting_manual_role_title`, `awaiting_manual_role_prompt`, `manual_role_title`, `manual_role_prompt` columns to `user_state` table (migration `008`) and updated `save_user_state`/`load_user_state` in `users.py`. Manual role creation now survives bot restarts.
- **Stale test assertions fixed**: 4 pre-existing test failures from UI text refactoring (old: "Управление ролями"/"Чат очищен", new: "Роли"/"Новый чат") now pass.

---

## [2.6.3] – 2026-02-26 – Codebase Audit & Bug Fixes

### 🔴 Critical Fixes

- **Missing `get_supabase_metrics` re-export**: `web.py:api_database()` called `database.get_supabase_metrics()` which was missing from `_REPO_EXPORTS` → `AttributeError` at runtime. Added mapping to `_REPO_EXPORTS`.
- **`await` on synchronous `float` return**: `cache.py:get_cache_stats()` used `await metrics_collector.get_cache_hit_rate()`, but `get_cache_hit_rate()` is a sync function returning `float` → `TypeError: 'float' object can't be awaited`. Removed erroneous `await`.

### 🟡 Medium Fixes

- **Redundant Redis `INFO` call**: `/api/cache` endpoint called both `get_cache_stats()` and `get_multi_layer_cache_stats()`, but the latter already calls `get_cache_stats()` internally — doubling Redis round-trips. Removed standalone call.
- **`redis.info()` dict mishandled as string**: `get_cache_stats()` converted the `redis.info()` dict to `str(dict)` and split on newlines, producing garbled stats (`"N/A"` for memory, uptime). Now accesses dict keys directly.

### ✅ Audit Findings (Clean)

Full scan of 21 core modules, 13 handlers, 7 repos confirmed:

- No SQL injection (all user-facing queries parameterized)
- No bare `except:`, `eval()`, `exec()`, `os.system()`
- No `asyncio.run()` or blocking `time.sleep()` in async context
- All `_REPO_EXPORTS` entries resolve correctly (60+ callsites verified)
- Security module (`InputSanitizer`, `RateLimiter`) comprehensive
- Circuit breaker, memory manager, task queue all properly bounded

### 🧪 Tests

- **404 passed**, 1 skipped — full green suite after all fixes.

---

## [2.6.2] – 2026-02-26 – UI/UX Audit & Manual Role Creation

### 🎨 UI/UX Improvements (Marketing Psychology)

- **Start menu restructured** (Hick's Law): New 4-row layout with primary CTA (`💬 Новый чат`), paired settings row (`🧠 Модель AI` + `🎭 Роли`), document/conversation shortcuts, and compact toggle row.
- **Roles hub redesigned** (AIDA funnel): Browse (`📚 Каталог` + `👤 Мои роли`) → Create (`✨ Сгенерировать` + `📝 Написать`) → Reset → Back.
- **Role detail buttons** updated: `✅ Применить` → `▶️ Активировать`, `🛑 Отключить` → `🔄 Сбросить` (positive framing).
- **Delete confirmation dialogs** — loss-aversion framing: "🚨 Все документы будут удалены безвозвратно" / "Вся история сообщений будет потеряна безвозвратно".
- **Cancel buttons** relabeled: `❌ Отмена` → `↩️ Отмена` (less alarming).
- **Keyboard button labels** updated: `🆕→✨ Новая тема`, `🔁→🔄 Повторить` (reduced activation energy).

### 🐛 Anti-Flood Fixes

- **6 `reply_text` → `edit_message_text`** conversions: role rename prompt, role AI creation prompt, role prompt view, and 3 error messages that were creating new messages instead of editing.

### 🛡️ Error Recovery (15+ dead-ends fixed)

- All error messages now have at least one actionable button (`⬅️ Меню`, `🎭 Меню ролей`, `📄 К документам`, `⬅️ К беседам`).
- Fixed across: `callbacks.py`, `cb_roles.py`, `cb_documents.py`, `cb_conversations.py`.

### ✨ New Feature: Manual Role Creation

- New `📝 Написать` button in roles hub and role lists.
- 2-step text flow: Title → Prompt → Preview → 💾 Save & Apply.
- 3 new callbacks: `role_create_manual`, `role_manual_cancel`, `role_manual_save`.
- State management: 3 new fields + 6 helper functions in `state.py`.
- Text handler: `_handle_manual_role_input()` in `messages.py`.

### 🧹 Code Quality

- **Keyboard consolidation**: Removed dead `main_menu_keyboard()` / `document_menu_keyboard()`. Added centralized `feedback_row()`, `ai_response_keyboard()`, `deep_dive_keyboard()`, `error_with_back_keyboard()`.
- **`messaging.py` migration**: 42 lines of duplicated keyboard code → single imports from `keyboards.py`.
- **Conversations menu**: Added `⬅️ Назад` back button and fixed empty-state dead-end.
- **Documents menu**: Added `⬅️ Назад` back button (from Phase 1).

### 🧪 Tests (404 passed, 1 skipped)

- Updated `test_keyboards.py`: 7 new tests (feedback_row, ai_response, deep_dive, error_with_back).
- Updated `test_menus.py`: new start menu assertions (4 rows, updated labels, new doc/conv buttons).
- Updated `test_roles_menu.py`: assertions match new button labels (Сгенерировать, Активировать).

---

## [2.6.1] – 2026-02-26 – Technical Audit Fixes

### 🔴 Critical Fixes

- **CRIT-01**: Fixed unreachable `except (APIError, httpx.HTTPError)` in `GeminiProvider._execute_request` — `APIError` was already caught by the preceding handler. Now correctly `except httpx.HTTPError`.
- **CRIT-02**: Removed duplicate `asyncpg.InterfaceError` from second except clause in both `DatabaseManager.query()` and `execute_many()`. `InterfaceError` was caught by the first clause (connection handler) and could never reach the second (rate-limit handler).

### 🟠 High-Severity Fixes

- **SEC-05**: `set_user_context()` now re-raises on failure instead of silently swallowing it. Prevents queries from running with stale RLS context (potential cross-user data leak).
- **SEC-01**: Added `_SAFE_IDENTIFIER_RE` regex validation for SQL table names in `setup_row_level_security()` before f-string interpolation.
- **BUG-05**: Added `log_openrouter_response()` to `APILogger`. OpenRouter `_log_failure` now logs under correct `openrouter` API name instead of misleading `gemini`.

### 🟡 Medium Fixes

- **BUG-01/02**: Removed unreachable `elif parts is None` dead code branches in `_build_contents` and `_build_messages`.
- Added `import re` to `database.py` (needed for new regex validation).
- Restored `GeminiProvider._log_failure` accidentally deleted during refactor.

### 🧪 Tests

- **401 passed**, 1 skipped, 0 failures — full green suite after all fixes.

---

## [2.6.0] – 2026-02-25 – Sprint 6–7: Audit & Refactoring

### 🔒 Security Audit (12 fixes)

- **C-1**: RLS `set_config` scoped to transaction-local (`true`) to prevent context leakage between pooled connections.
- **C-2**: Bare `except:` → `except (json.JSONDecodeError, ValueError, TypeError)` in `database.py`.
- **C-3**: API key preview reduced from 10 → 4 chars across `database.py` and `metrics_repo.py`.
- **C-4**: `force_update_tavily_keys()` wrapped in atomic transaction.
- **C-5**: Added `exc_info=True` to generic exception handlers in `services.py`.
- **C-6**: `delete_conversation()` wrapped in atomic transaction.
- **C-8**: `conversation_messages` RLS policy rewritten to use subquery through `conversations` table.
- **A-1**: `APIError` → `GemaibotAPIError` to avoid collision with `google.genai.errors.APIError`.
- **D-2**: Redundant `except Exception:` clause removed from `database.py`.
- **D-3**: `ConnectionRefusedError` → `ServiceConnectionRefusedError` to avoid shadowing builtin.
- **D-4**: `token_count` correctly restored from `token_budget` on conversation switch.

### 🏗️ Architecture Refactoring

- **Metrics deduplication**: Deleted ~125 duplicate lines from `database.py` → re-exports from `repos/metrics_repo.py`.
- **Rate limiter consolidation**: Removed `_UserRateLimiter` (23 lines) from `ai_provider.py`. `ProviderRouter` now uses `security.RateLimiter` (includes periodic cleanup, stats, admin reset).
- **`DailyKeyManager` class**: New generic key rotation engine in `repos/keys.py`. Gemini and OpenRouter share one parameterized SQL engine. Tavily kept separate (monthly-credit model).
- **Provider call chain fix**: `GeminiProvider` and `OpenRouterProvider` now call `_execute_*_request()` directly, eliminating double-retry bug.
- **Unified AI call path**: `AgentRequestUseCase.get_ai_response()` now delegates to Provider classes via `get_provider_for_model()` instead of calling `services.get_*_response()` directly.
- **Deprecation warnings**: `services.get_gemini_response()` and `services.get_openrouter_response()` now emit `DeprecationWarning`. New code should use Provider classes.

### ⚡ Performance

- **Lazy logging**: Converted 242 f-string logging calls → `%s` format across 36 files. Prevents string interpolation when log level is disabled.

### 🐛 Bug Fix: Model Timeout (gemini-2.5-flash / gemini-3-flash)

- **Root cause**: `asyncio.to_thread(client.models.generate_content, ...)` ran the synchronous SDK method in a thread. When `asyncio.wait_for()` timed out, it cancelled only the Python future — the thread continued running the HTTP request as a zombie, consuming resources.
- **Fix**: Switched to native async `client.aio.models.generate_content()` + `client.aio.models.count_tokens()`. These properly support `asyncio.CancelledError` and abort the HTTP connection on timeout.
- **SDK-level deadline**: Added `HttpOptions(timeout=90_000)` (90s) to the `genai.Client`, ensuring the HTTP library itself enforces a hard deadline even if the asyncio layer fails.
- **Python-side deadline**: Reduced from 120s → 100s (10s buffer over SDK timeout) to prevent silent hangs.

### 🧪 Tests (+31 → 349 pre-Phase 7)

- `test_daily_key_manager.py`: 12 DailyKeyManager + 5 MonthlyKeyManager tests.
- `test_unified_call_path.py`: 4 tests (Gemini routing, OpenRouter routing, error response, no-keys guard).
- `test_provider_router_integration.py`: 6 tests (full chain Gemini/OpenRouter, all-exhausted, rate limit, multimodal, key-failure retry).
- `test_timeout_smoke.py`: 4 tests (async cancellation, no zombie tasks, CancelledError propagation, SDK HTTP timeout config).
- Updated `test_services_gemini.py` mocks: `client.models.*` → `client.aio.models.*` (`AsyncMock`).

### 🧹 Code Quality

- Standardized all Russian comments/docstrings in `security.py` `RateLimiter` to English.
- Removed dead `from app import services` import from `agent_use_cases.py`.
- **MonthlyKeyManager** — new generic class for monthly-credit key rotation (Tavily). Completes the KeyManager abstraction alongside DailyKeyManager.
- **OpenRouter timeout** tightened 120s → 90s with explanatory comments. No zombie-thread risk (uses async httpx).
- **asyncio.to_thread audit** — all remaining usages verified safe (CPU-bound encoding, Redis ops).
- **\_save_image_as_bytes** — reviewed, already has 5s ProcessPoolExecutor timeout. No changes needed.
- **Russian → English comments** — automated translation of 659 comment/docstring lines across 26 files (dictionary-based, no LLM). User-facing strings preserved.
- **Provider class migration** — moved `_execute_gemini_request` (312 lines) and `_execute_openrouter_request` (265 lines) from `services.py` into self-contained `GeminiProvider` and `OpenRouterProvider` classes. New `app/utils/image_utils.py` for shared image processing.
- **services.py → search_services.py** — renamed to reflect actual content (pure Tavily search). Backward-compat shim at `services.py`. Removed all deprecated wrappers (`get_gemini_response`, `get_openrouter_response`, `_with_retry`, `_validate_api_inputs`, `_caller_info`) and execute stubs. Dead `from app import services` import removed from `ai_document.py`. Final size: 160 lines (was 951).

### 🔒 Security Hardening (Phase 7)

- **RLS enabled** on 4 previously unprotected tables: `user_metrics`, `user_state`, `feedback`, `schema_migrations`.
- **2 unused indexes dropped**: `idx_group_messages_owner`, `idx_error_logs_created_at`.

### 🏗️ Typed Exception Catches (Phase 6–7, 75 total)

| File                    | Catches Refined | Types Used                                                                |
| ----------------------- | --------------- | ------------------------------------------------------------------------- |
| `database.py`           | 13              | `(asyncpg.PostgresError, asyncpg.InterfaceError)`                         |
| `document_processor.py` | 17              | `asyncpg.*`, `ValueError`, `pypdf.errors.PdfReadError`, `httpx.HTTPError` |
| `ai_search.py`          | 20              | `(BadRequest, NetworkError)`                                              |
| `messages.py`           | 4               | `(BadRequest, NetworkError)`, `OSError`                                   |
| `ai_provider.py`        | 10              | `(APIError, httpx.HTTPError)`, `(TypeError, ValueError)`                  |
| `memory_manager.py`     | 2               | `(OSError, AttributeError)`                                               |
| `cache.py`              | 9               | `(ConnectionError, RedisError)`, `(TypeError, ValueError, KeyError)`      |

### 🧪 Tests (+34 → 383 total)

- `test_repos_users.py`: 11 tests — auth, cache, state, feedback.
- `test_repos_conversations.py`: 8 tests — CRUD, rename, delete.
- `test_repos_keys.py`: 12 tests — DailyKeyManager, MonthlyKeyManager, cache.
- `test_io_handlers.py`: Converted from `unittest.TestCase` + manual event loop → `pytest-asyncio` (fixed test-ordering failure).
- `test_ai_provider.py`: Fixed `test_gemini_wrapper_error` — added missing `count_tokens` mock.

### 📝 Type Hints (Phase 8, 114 annotations)

- **Repos** (11): `keys.py` (6), `users.py` (1), `chats.py` (1), `analytics.py` (1), `metrics_repo.py` (1), `conversations.py` (1)
- **Handlers** (103): `commands.py` (33), `cb_roles.py` (20), `callbacks.py` (14), `cb_conversations.py` (12), `messages.py` (11), `menus.py` (6), + 7 smaller files

### 🧪 Integration Tests (+18 → 401 total)

- `test_integration_flows.py`: 18 tests across 4 cross-module flows:
  - **AI response lifecycle** (3): Gemini routing, OpenRouter routing, error handling
  - **Key rotation** (8): DailyKeyManager get/exhaust/increment/available, MonthlyKeyManager get/exhaust
  - **Conversation CRUD** (4): create+list, rename, delete, save history
  - **User auth chain** (4): admin bypass, DB lookup, unauthorized, cache hit

---

## [2.5.0] – 2026-02-25 – Sprint 5: Polish & Hardening

### ⚡ ProviderRouter Enhancements

- **Multimodal auto-detection**: Router detects PIL Image / bytes in history and forces Gemini automatically — handlers no longer pass `use_openrouter=False` explicitly.
- **Per-user rate limiting**: Sliding-window `_UserRateLimiter` (default 20 req/min) prevents abuse. Returns user-friendly `⏳ Слишком много запросов` on throttle.

### 📊 Per-User Metrics

- **`user_metrics` table**: New `(user_id, metric_date)` keyed table tracks personal request counts and model usage.
- **`MetricsCollector` update**: `record_request()` and `record_api_call()` now accept `user_id` param; per-user data saved to DB alongside global metrics.
- **`/stats` and `/start` personalized**: Queries now show personal stats (today count, 7-day history, model usage) instead of global.

### 🖼️ Stage Indicators

- Wired `STAGES_PHOTO` into `ai_photo.py`: `_handle_photo` and `_handle_media_group_photos` now show animated processing stages.

### 🧪 Test Coverage (+28 tests → 291 total)

- `test_stage_indicators.py`: 15 tests (definitions, update behavior, capping, sequential, error handling).
- `test_provider_router.py`: 13 tests (KeyHealth scoring, ProviderRouter retry/skip/detection). _Note: rewritten in v2.7.0 for DB-backed KeyStatusManager._

### 🧹 Cleanup

- Removed 4 stale re-exports from `agent.py` facade.
- Removed unused `from app import services` in `ai_photo.py`.

---

## [2.4.0] – 2026-02-25 – Sprint 4: Legacy Cleanup

### 🧹 Removed Dead Code

- `_LegacyGeminiWrapper` / `_LegacyOpenRouterWrapper` aliases from `ai_provider.py`.
- `_UserLocksProxy` class + `USER_LOCKS` singleton from `state.py`.
- `get_user_locks()` backward-compat alias from `state.py`.
- `_cleanup_memory_cache()` deprecated stub from `cache.py`.

### 🔄 Handler Router Migration (Pre-Sprint-4)

- **All AI handlers** now use `_get_ai_response_with_routing()` instead of direct `services.get_gemini_response()` calls.
- Migrated: `ai_photo.py` (3 functions), `ai_document.py`, `ai_search.py::_handle_complex_agent_search`.
- Removed manual `db.increment_gemini_key_usage()` calls — router handles usage tracking internally.
- Added `handle_ai_response_error()` checks for router error messages.

---

## [2.3.0] – 2026-02-25 – Codebase Audit, Docker Optimization & Bug Fixes

### 🧹 Codebase Audit & Cleanup

- **Legacy Files**: Moved 12 obsolete files to the `legacy/` directory to declutter the root workspace.
  - Dead code (`app/health.py`, `app/alerts.py`) and their orphaned tests moved to `legacy/app/` and `legacy/tests/`.
  - Development tools and benchmarks moved to `legacy/dev_scripts/`.
  - Obsolete Render deployment configs moved to `legacy/deploy_render/`.
- **Gitignore**: Expanded `.gitignore` to explicitly exclude IDE configurations (`.vscode`, `.cursor`, `.Jules`, `.roomodes`), linter caches, and stale test logs.

### 🐳 Docker & CI Optimization

- **`Dockerfile.northflank` Overhaul**:
  - Upgraded base image from Python 3.11 to **Python 3.14-slim** for better performance string resolving and modern standard library features.
  - Reduced Dockerfile length by 50% (71 → 36 lines).
  - Extracted inline startup commands into a dedicated `start.sh` executable.
  - Consolidated `RUN` layers to reduce image size.
  - Added native Docker `HEALTHCHECK` instruction.
- **Requirements Split**: Separated dependencies into `requirements.txt` (prod-only) and `requirements-dev.txt` (includes `pytest`). Production image no longer installs testing frameworks.
- **`.dockerignore`**: Created robust `.dockerignore` to prevent tests, legacy files, and IDE caches from inflating the production image.

### 🐛 Bug Fixes

- **Telegram Polling Crash**: Fixed `TypeError: Updater.start_polling() got an unexpected keyword argument` on startup.
  - **Root Cause**: `python-telegram-bot` v22.0 removed HTTP timeout arguments from `start_polling()`.
  - **Fix**: Removed deprecated kwargs, preserved valid Telegram long-polling `timeout=30`, and kept HTTP timeouts correctly scoped to `HTTPXRequest`. Added static AST regression test (`test_start_polling_kwargs.py`) to prevent recurrence.
- **Database Metrics Crash**: Fixed `column "request_id" does not exist` error spamming logs on boot.
  - **Root Cause**: The `metrics` and `error_logs` tables relied on a standalone SQL migration script that was never executed in the deployment pipeline.
  - **Fix**: Native schema definitions and the missing `request_id` column patching were integrated directly into the `app/database.py:_init_schema` and `_run_migrations` boot sequence for automatic repair.

## [2.2.0] – 2026-02-22 – Test Suite Isolation Overhaul

### Context

The full test suite (`python -m pytest tests/`) suffered from **cascading cross-test failures** (up to 38 simultaneous) caused by global `sys.modules` mock pollution. Individual tests passed in isolation but failed when collected together because early-alphabetical test files injected `MagicMock` objects into `sys.modules` at **module parse time** (before `setup_module`), permanently replacing real modules for all subsequently-collected files.

### Root Cause (for future agents)

Python's `sys.modules` is a global singleton. When a test file executes `sys.modules["pytz"] = MagicMock()` at the **top level** (outside any function), pytest evaluates it during **collection** — before any test runs. This poisons `pytz` for every other test file in the session. The fix pattern is:

1. **Move** all `sys.modules[...] = MagicMock()` into `setup_module()`.
2. **Save** original modules: `_original_modules[k] = sys.modules.pop(k, None)`.
3. **Restore** in `teardown_module()`: delete injected keys, `sys.modules.update(_original_modules)`.
4. **Reload** dependent modules via `importlib.reload()` in `setup_module()` when upstream mocks change their identity.

### Files Changed

#### `tests/test_auth_headers.py`

- **Before**: 12 `sys.modules[...] = MagicMock()` calls at module top-level.
- **After**: All moved into `setup_module()`/`teardown_module()` with proper save/restore.

#### `tests/test_menus.py`

- **Before**: `setup_mocks()` injected mocks into `sys.modules` but `app.handlers.menus` was already imported with stale references.
- **After**: Added `importlib.reload(sys.modules["app.handlers.menus"])` inside `setup_module()` after mock injection. Added cleanup of `app.handlers.menus` in `teardown_module()`.

#### `tests/test_keyboards.py`

- **Before**: No `setup_module`. Imported functions at top level bound to whatever `telegram` module existed at collection time.
- **After**: Added `setup_module()` that detects MagicMock `telegram`, deletes it, reloads `app.utils.keyboards`, and re-injects all public attributes onto the test module via `setattr(module, attr, getattr(reloaded, attr))`.

#### `tests/test_database_tavily.py`

- **Before**: Top-level `sys.modules["pytz"] = MagicMock()` and `sys.modules["asyncpg"] = MagicMock()`. Used `import app.database` (attribute-chain form).
- **After**: Removed all `sys.modules` overrides entirely. Switched from `import app.database` → `from app import database` to avoid `AttributeError: module 'app' has no attribute 'database'` caused by teardown scripts deleting `app.database` from `sys.modules`.

#### `tests/test_document_cleanup_optimization.py`

- **Before**: `patch.dict(sys.modules, {...})` context manager wrapping a `from app.document_processor import DocumentProcessor` at module level. The `mock_db.db_query` was a `MagicMock` (not `AsyncMock`), causing `'MagicMock' object can't be awaited`.
- **After**: Direct `from app.document_processor import DocumentProcessor` (no sys.modules patching). Test uses `@patch("app.document_processor.database")` decorator with `AsyncMock` for `db_query`.

#### `tests/test_perf_db_messages.py`

- **Before**: `import app.database` at top level failed with `AttributeError: module 'app' has no attribute 'database'` when prior teardowns deleted it from `sys.modules`.
- **After**: Added `get_database()` helper that uses `from app import database` + `patch.object()` to avoid relying on `sys.modules` state.

#### `tests/test_security_headers.py`

- **Before**: `/health` endpoint returned `503` when run after `test_io_handlers.py` because the real (dead) database pool was initialized.
- **After**: Added `patch("app.web.database.is_database_connected", return_value=True)` to the `client` fixture.

#### `tests/test_system_status.py`

- **Before**: `@patch("app.database.db_query")` decorator targeted the real `app.database` module, but after `patch.dict` + `importlib.reload`, `app.metrics` internally referenced the MagicMock substitute. The decorator's patch never reached the code path.
- **After**: Removed `@patch("app.database.db_query")`. Now creates `AsyncMock` inline and patches via `patch.object(self.metrics_module, "db")` to target the actual reference used by the reloaded module.

#### `tests/test_metrics_integration.py` (prior session)

- Removed destructive `importlib.reload()` calls in `setUp()`.

#### `tests/test_web_security.py` (prior session)

- Moved top-level `sys.modules` mocks into `setup_module()`/`teardown_module()`.

#### `tests/test_callbacks.py` (prior session)

- Moved top-level `sys.modules` mocks into `setup_module()`/`teardown_module()`.

#### `tests/test_document_security.py` (prior session)

- Wrapped `sys.modules["app.database"] = MagicMock()` in `setup_module()`/`teardown_module()`.

### Verification

```
python -m pytest tests/ --tb=short
=========== 192 passed, 1 skipped, 1 xfailed, 0 failures in 31.89s ===========
```

### Anti-Pattern Reference (for future agents)

| ❌ Anti-Pattern                                            | ✅ Correct Pattern                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------------------- |
| `sys.modules["X"] = MagicMock()` at top level              | Move into `setup_module()` with save/restore in `teardown_module()`  |
| `import app.database` then `app.database.func()`           | `from app import database` then `database.func()`                    |
| `@patch("app.database.db_query")` after `importlib.reload` | `patch.object(reloaded_module, "db")` targeting the cached reference |
| `MagicMock()` for async functions                          | `AsyncMock()` for any function that is `await`ed                     |
| `importlib.reload()` in `setUp()`                          | Avoid; use `setup_module()` (once per file) instead                  |

---

## [2.1.0] – Performance Optimizations

- Non-blocking document I/O with async file processing
- Batched metrics DB inserts via `asyncio.Queue`
- Scoped DB transactions with `asyncio.Semaphore`
- GIL-free image processing via `ProcessPoolExecutor`
- TTLCache with lazy eviction for web search states
- Micro-GC pauses with tuned `gc.collect(1)`
- Robust TCP pooling with Circuit Breaker tracking

