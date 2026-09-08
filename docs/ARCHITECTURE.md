# Architecture — GemAI Bot v2

Reviewed against checkout `8fc19516` on 2026-09-08. This describes implementation,
not a claim that a particular VPS or provider has been tested today.

## Runtime and request ownership

Python 3.14 runs PTB and Quart/Hypercorn in one async application lifecycle.
`bot.py` registers handlers, public command menus and scheduled jobs, initializes
storage, and drains owned work on shutdown. Without `WEBHOOK_URL` it polls.
With it, the webhook path contains a derived hash rather than the bot token;
configured `WEBHOOK_SECRET_TOKEN` is checked before accepting updates.
`UserScopedUpdateProcessor` bounds concurrency and serializes user-scoped work.
`app/webhook_dedupe.py` is distinct from message double-tap deduplication in
`app/middleware/dedup.py`.

```text
Telegram update → bot.py / handlers → context + provider routing
                                      ↓ typed generation events
                            response_delivery → Telegram transport
                                      ↓
                              immutable outcome
```

## Navigation map

| Boundary | Sources and responsibility |
| --- | --- |
| Configuration | `app/config.py`: Pydantic BaseModel, explicit environment loading, model roles and hot reload; `app/repos/models_repo.py`: explicit catalog overrides |
| Commands and intent | `app/bot_commands.py`: public menu/help; `app/handlers/`: behavior; `app/intent_router.py`: deterministic weather/currency/crypto dispatch |
| Providers | `app/providers/router.py`: chat routing, keys and fallback; `stream_types.py`, `request_factory.py`, `typed_payloads.py`: typed contract and payload translation |
| Delivery | `app/response_delivery/`: coordination, presentation, progressive rendering, final outcome and action ownership |
| Research | `app/core/agentic.py`, `app/search_services.py`, `app/search_jina.py`: bounded research/tool execution |
| Context | `app/context/`, `app/prompt_registry.py`: history, budgets, summaries and prompt templates |
| Documents | `app/documents/`, `app/document_processor.py`: extraction, chunking, persistence and Q&A |
| Persistence | `app/database.py`: pool/lifecycle; `app/repos/`: domain queries; `app/db/`: migrations, validation, RLS and seed |
| State/concurrency | `app/state.py`: local state and DB persistence; `app/adapters/concurrency.py`: Redis/local semaphores; `app/utils/background_tasks.py`: tracked detached work |
| Daily products | `app/games/`: Crocodile, 2048, trivia and shared daily AI authoring; matching handlers/repositories own Telegram delivery and storage |
| Astrology | `app/astro.py`, horoscope handlers, `app/tarot*.py`, `app/natal/`: horoscopes, tarot, natal calculation/reporting |
| Web surfaces | `app/web.py`: dashboard/admin; `app/web_miniapp.py`: Mini App APIs and live/game flows; `app/web_reader.py`, `app/web_natal.py`: long reads/reports |
| Observability | `app/request_context.py`, `app/metrics.py`, `app/prometheus.py`, `app/utils/logging_config.py`; `app/memory_manager.py` monitors process memory, not LTM |

Avoid static file-size/module-count maps: they drift without helping identify owners.

## Providers and model selection

Chat providers include Gemini, Opencode, OpenRouter and FreeTheAI. The provider
router is not a universal superclass/factory for every image, audio or game API.
Image providers, Gemini embeddings, TTS and Live Audio have specialized interfaces.
An explicit Crocodile text-model selection uses `google-genai` inside the game
boundary for both ordinary and daily games; auto mode retains its existing lanes.
See [Crocodile operations](pollinations-daily-croc.md).

`app/config.py` defines role defaults and ordered selectable lists. Unset/blank
`*_AVAILABLE_MODELS` uses defaults; `none` makes a selector intentionally empty.
Explicit v2 administrator overrides take precedence over the env baseline.
Internal role models need not appear in the user selector. An accepted model ID
does not prove account access, quota or current provider availability.

`imagen_provider.py` retains its historical filename/class but now uses Gemini
native image generation; old Imagen IDs are normalized compatibility inputs.
Pollinations generation is authenticated; no anonymous fallback is promised.

## Response delivery

A generation emits text deltas and one typed terminal event carrying completion,
failure/deferred state, usage, grounding and actual route. The coordinator owns
request cleanup; presentation prepares canonical versus displayed content; the
renderer owns progressive edits and final text/actions. Downstream handlers use
immutable receipts and must not overwrite the final keyboard.

Long content is checked after formatting/sanitization. Fallback order is Redis
Reader → **opt-in** Telegraph → Telegram split. Reader delivery succeeds only after
storage and displaying its action succeed. Reader cold-storage mirroring is also
gated by `TELEGRAPH_PUBLICATION_ENABLED=false` by default. Split actions attach to
the last message. See [ADR 0001](adr/0001-single-owner-ai-response-delivery.md).

## State and background work

`UserState` is a process-local LRU with PostgreSQL-backed persisted fields, a local
async lock and debounced persistence. Active tasks and last-message references
are process-local. Redis holds selected caches, distributed semaphores, deferred
work and game state; it does not replicate all Python state. Multi-replica safety
must be evaluated per subsystem.

Detached work uses tracked submission/retry helpers. Direct request-owned tasks
are valid when all completion/cancellation paths await cleanup. Synchronous pure
transformations need not become async. Existing logging uses both stdlib logging
and structlog bridging, not an unconditional structlog-only rule.

## Long-term memory

Private LTM is in `app/repos/memory*.py`, with pgvector `halfvec(768)`, hybrid
retrieval, graph traversal, tiered context and source-aware feedback. Query
thresholds, models and budgets belong to code, not an undated latency promise.

Consent epochs and renewable database leases prevent stale private-data work
from surviving revocation. Group messages are not implicitly captured into
private LTM. Deletion, expiry and retrieval use live tenant-scoped provenance.

Extraction and consolidation prepare provider results/embeddings before a
caller-owned transaction. `memory_graph_writer.write_graph` receives that
connection and an immutable plan with durable source IDs. Source rows, graph
mutations, normalized `memory_edge_sources`, compatibility snapshots and source
markers commit or roll back together. The writer does not acquire a hidden pool
connection or perform network I/O. See [ADR 0002](adr/0002-provenance-safe-memory-graph-writes.md).

## Database lifecycle and privacy limits

`scripts/migrations/*.sql` is discovered by the shared strict manifest validator.
Versions must be unique; filenames, nonempty SQL and UTF-8 are checked.
The complete ordered chain, not migration 000 alone, defines the current schema.
The standalone runner and startup stop on migration failure; startup additionally
validates runtime tables/columns and provisions RLS.

`scripts/migrate.py --check` checks pending versions, not every schema invariant
or migration checksum, and can create `schema_migrations`. Do not describe it as
a strictly read-only complete drift audit.

Current startup uses one `DATABASE_URL` for migrations and runtime. Superusers,
BYPASSRLS roles and ordinary table owners can bypass enabled RLS. Application
tenant scoping and consent checks remain necessary; a separate migrator/runtime
role deployment is not automatically supplied by this code.

Keys use Fernet derived from `ADMIN_SECRET`. Mini App initData, dashboard sessions,
webhooks and game/Live WebSockets have separate guards; rate limits and circuit
breakers are resilience controls, not a substitute for authentication.
Public Telegraph copies and shared report links require their own privacy handling.

## Build, CI and deployment

`pyproject.toml` requires Python `>=3.14,<3.15` and uv 0.12.6; `uv.lock` fixes the
graph. The production Dockerfile uses a digest-pinned Python 3.14 slim image,
locked non-dev dependencies, ffmpeg and a non-root application user.

`.github/workflows/ci.yml` defines lint/format, Mypy, unit/E2E, serial PostgreSQL
integration, offline container smoke and production dependency audit/SBOM/license
evidence. CI cancels superseded runs. Deployment serializes transitions with
`cancel-in-progress: false`, builds the successful CI SHA and runs migration and
health gates. Dependency-only rollback has a restricted eligibility check; it is
not a general code/schema rollback or proof of functional provider correctness.

The VPS workflow defines `tg-api`, `tg-bot` and `tg-media-cleanup`; database/Redis
are supplied separately. The root Compose file is a legacy single-bot local
alternative, not this production stack. See [README operations](../README.md#run)
and [dependency maintenance](../README.md#dependency-maintenance).

For exact commands, test-service safety and local hook installation, use
[CONTRIBUTING.md](../CONTRIBUTING.md). Historical pass counts are not current CI evidence.
