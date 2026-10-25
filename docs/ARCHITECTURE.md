# Architecture — GemAI Bot v2

Baseline review: checkout `f44541a7` on 2026-09-20. Runtime/error/shutdown corrections
were reviewed on 2026-10-23 against `25e25897` and the changes documented in the
[runtime audit](runtime-audit-2026-10-23.md). Later updates cover request cancellation,
state persistence, CI/deploy gates, sequential bounded TTS and
[direct speech requests](voice-and-tts.md). The 2026-10-25 audit additionally
checked startup supervision, state recovery, Reader rendering, Live leases,
2048 retry ownership and natal account generations. This describes implementation,
not a claim that a particular VPS or provider has been tested today.

## Runtime and request ownership

Python 3.14 runs PTB and Quart/Hypercorn in one async application lifecycle.
`bot.py` registers handlers, public command menus and scheduled jobs, initializes
storage, and drains owned work on shutdown. Failed database initialization stops
bootstrap. An unexpected Telegram or web-server task exit stops and awaits the
other owned services, then propagates failure so the process can be restarted.
Without `WEBHOOK_URL` it polls.
With it, the webhook path contains a derived hash rather than the bot token;
configured `WEBHOOK_SECRET_TOKEN` is validated by `app/webhook_security.py`
(1–256 ASCII letters/digits/underscore/hyphen) and checked with constant-time
comparison before accepting updates. Empty configuration disables that header guard.
`UserScopedUpdateProcessor` bounds concurrency and serializes stateful
user-scoped work. Stateless inline preview queries bypass the per-user lock so
long callbacks cannot make their Telegram query IDs expire before answering.
`app/webhook_dedupe.py` is distinct from message double-tap deduplication in
`app/middleware/dedup.py`.

Webhook admission ownership was updated on 2026-10-10. After PTB starts,
`bot.py` registers the route and uses `app/webhook_admission.py` to serialize
capacity checks, dedupe claims, origin registration and queue handoff. A caller
cancelled while waiting starts no claim. Once admission starts, its owned child
finishes before caller cancellation is restored and the lock is released.
Lifecycle cleanup closes admission before yielding, drains active admission and
then stops PTB. The queue remains process-local: process crashes and unknown
Redis acknowledgements are outside this cooperative cancellation guarantee.
See the [dated verification report](webhook-admission-ownership-2026-10-10.md)
for tested scenarios and evidence limits.

Required text/photo/edit/voice/draw/suggestion workers use bounded task admission. Capacity
rejection produces a visible overload result and retires the placeholder's
heartbeat and admission; optional typing/cleanup work remains best effort.
The stop callback checks the active owner, message/chat identity and pending
heartbeat before cancellation, rechecks ownership after network waits, and drains
the cancelled worker before changing its reply. A superseding request retains
its own admission and final output.

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
| Providers | `app/providers/router.py`: chat routing, keys and fallback. Interactive Gemini chat hedges up to two model lanes after a short delay; each lane retains its key race, and the first visible text wins. `stream_types.py`, `request_factory.py`, `typed_payloads.py`: typed contract and payload translation |
| Delivery | `app/response_delivery/`: coordination, presentation, progressive rendering, final outcome and action ownership |
| Speech | `app/voice_intent.py`: deterministic supplied-text selection versus generated voice replies; `app/handlers/msg_speech.py`: direct enqueue/clarification; `app/voice_engine.py`: bounded private queue, synthesis and voice delivery |
| Research | `app/core/agentic.py`, `app/search_services.py`, `app/search_jina.py`: bounded research/tool execution |
| Context | `app/context/`, `app/prompt_registry.py`: history, budgets, summaries and prompt templates |
| Documents | `app/documents/`, `app/document_processor.py`: extraction, chunking, persistence and Q&A |
| Persistence | `app/database.py`: pool/lifecycle; `app/repos/`: domain queries; `app/db/`: migrations, validation, RLS and seed |
| State/concurrency | `app/state.py`: local state and DB persistence; `app/adapters/concurrency.py`: Redis/local semaphores; `app/utils/background_tasks.py`: tracked detached work |
| Daily products | `app/games/`: Crocodile, 2048, trivia and shared daily AI authoring; matching handlers/repositories own Telegram delivery and storage. `crocodile_daily_preferences.daily_game` stores a player's optional choice; the admin mode is the fallback. The scheduler prepares all three games, with a seven-day lookahead for the admin default and tomorrow for the others. |
| Daily preparation | `app/games/daily_preparation.py`: read-only readiness, managed Easy/Hard preparation, Redis lease/status and local fallback; `crocodile_daily.py`: automatic image quota and explicit admin bypass |
| Astrology | `app/astro.py`, horoscope handlers, `app/tarot*.py`, `app/natal/`: horoscopes, tarot, natal calculation/reporting |
| Web surfaces | `app/web.py`: dashboard/admin; `app/web_miniapp.py`: Mini App APIs and live/game flows; `app/web_reader.py`, `app/web_natal.py`: long reads/reports |
| Observability | `app/observability/`: envelope, context, redaction, writer, lifecycle mappings, operational metrics and incident export; `app/utils/logging_config.py`: bootstrap/compatibility; `app/request_context.py`: adapter; `app/metrics.py`: product metrics; `app/prometheus.py`: runtime exporter |

Avoid static file-size/module-count maps: they drift without helping identify owners.

## Providers and model selection

Chat providers include Gemini, Opencode, OpenRouter and FreeTheAI. The provider
router is not a universal superclass/factory for every image, audio or game API.
Image providers, Gemini embeddings, TTS and Live Audio have specialized interfaces.
Direct recitation bypasses chat context assembly, intent classifiers and text
generation. Long direct sources retain their Telegram message IDs and enqueue
one job per full original message in source order. A verified small final tail
can join its predecessor; a notice identifies the shared audio reply. It preserves the selected text
until shared formatting cleanup. Contiguous collected sources are cleaned once
with character provenance across physical cuts; surviving text keeps its original
reply target and bypasses a second cleanup in the FIFO job. Gemini counts actual
input tokens on the selected TTS model, including explicit metadata material.
Hash-only receipts remain scoped to the source/job and avoid duplicate counting
after merge admission or key rotation. Audio output limits are enforced by the
API; explicit truncation can trigger subdivision. The engine synthesizes necessary pieces sequentially and combines their PCM into
one file per job. Explicit truncation can subdivide the same source within
bounded limits; partial jobs are never delivered. Key/model retries are
sequential; ElevenLabs splits only beyond its model's text limit. TTS has
independent RPM/RPD admission and does not inherit chat key racing. See
[runtime voice controls](runtime-controls.md#голосовые-сообщения-и-tts).
An explicit Crocodile text-model selection uses `google-genai` inside the game
boundary for both ordinary and daily games; auto mode retains its existing lanes.
See [Crocodile operations](pollinations-daily-croc.md).

Agentic research is another specialized SDK boundary: `app/core/agentic.py`
executes Gemini tool iterations and final synthesis directly; `ai_search.py`
owns its model/key fallback. One `ResearchBudget` shares page admission, at most
three attempts and a deadline across the fallback chain. Async calls use the
remaining time, with a synthesis reserve of `min(15 seconds, timeout / 6)`.
Cancellation is cooperative and cleanup can take additional time. The token cap
controls admission rather than billing: an admitted call and final synthesis can
exceed it, and missing usage reserves 4,000 estimated tokens. Initial key lookup
and final Telegram delivery remain outside this budget.

`app/config.py` defines role defaults and ordered selectable lists. Unset/blank
`*_AVAILABLE_MODELS` uses defaults; `none` makes a selector intentionally empty.
Explicit v2 administrator overrides take precedence over the env baseline.
Internal role models need not appear in the user selector. An accepted model ID
does not prove account access, quota or current provider availability.

Typed failure dispositions distinguish another key, another model, retry later
and a terminal request error. A model-specific access denial can advance the
configured model chain; an invalid payload does not repeat the same request
through healthy keys. `KeyDisposition.UNCHANGED` preserves key health.

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

Reader treats Markdown and table-of-contents entries as untrusted content.
Inline JavaScript values use Jinja's HTML-safe JSON serialization; rendered
Markdown links allow HTTP, HTTPS, mailto and relative destinations. Script-bearing
URL schemes are rendered as text instead of active links.

## State and background work

`UserState` is a process-local LRU with PostgreSQL-backed persisted fields, a local
request lock and debounced persistence. A failed initial read is propagated and
retried on the next access; it is not treated as an absent saved row. Document,
role and suggestion entry points hydrate before reading or mutating persisted fields. Failed or
cancelled ordinary writes retain a retry marker for the latest snapshot. The
final shutdown flush starts no further retries and reports failed saves.
A separate per-state persistence lock
serializes snapshots and their writes, including database retries, so an older
write cannot overwrite a newer snapshot. Queued writes recheck account-erasure
invalidation after acquiring that lock. Account erasure acquires the same local
persistence lock before deleting data and holds it through post-commit state
invalidation. This orders an already-running SQL retry before deletion, so it
cannot repopulate a recreated account in the same process. Waiting for this lock
has a ten-second admission limit; failure leaves the account and state intact.
Once destructive work starts, its owned child drains commit acknowledgment and
local invalidation before caller cancellation is restored. An unknown commit
outcome invalidates the old local snapshot and propagates failure.
Active tasks and last-message references
are process-local. Redis holds selected caches, distributed semaphores, deferred
work and game state; it does not replicate all Python state. Multi-replica safety
must be evaluated per subsystem.

Detached work uses tracked submission/retry helpers. Direct request-owned tasks
are valid when all completion/cancellation paths await cleanup. Synchronous pure
transformations need not become async. Stdlib logging, structlog, warnings and
uncaught exceptions converge on one bounded NDJSON pipeline. Stable owner events
link ingress, provider attempts, immutable delivery outcomes, jobs and storage;
legacy strings retain the same envelope. See [logging operations](logging.md).

Shutdown stops producers and the deferred queue, drains accepted background
jobs and dependent writes under one deadline, then flushes final private-memory
and debounced state writes before closing HTTP/Redis resources. Polling and
webhook stop operations retain cancellation ownership. State timers submit to
the tracked task manager; capacity rejection retains their marker and retries
admission so final cleanup can still persist the latest state.

The process memory monitor is shared by startup, dashboard statistics and
shutdown. Its periodic psutil sampling and the memory API sample in worker
threads so CPU measurement does not pause the Telegram/HTTP event loop.
An in-memory queue retry that cannot regain capacity becomes a terminal failed
task with a capacity event; it is never advertised as a scheduled retry without
a queue entry. These paths were regression-tested in the
[codebase audit](codebase-audit-2026-10-23.md).

Daily Trivia submissions evaluate the selected answer and score against the
result's pinned question revision. Main and super-game writes share a consistent
main-row-first lock order inside a short transaction; a super completion updates
both result rows atomically. Repeated answers are idempotent, skipped/conflicting
answers are rejected, and the Mini App restores active progress with the same
question snapshot. Telegram result notifications run after the write commits.

Daily 2048 and daily/classic Crocodile recheck a guess or move's `pending_id` and
record its replay response under the same game mutation lock. Accepted actions
finish before socket cancellation releases that lock. Classic result broadcast
and bounded finalization also remain owned through cancellation; terminal replay
is restored after finalization clears the game cache. Daily result messages and
the optional thermometer update remain best effort.
This is bounded runtime replay, not database-backed exactly-once delivery across
process crashes or an unavailable Redis cache. The existing Redis mutation lease
lasts 60 seconds without renewal; local regressions do not establish cross-replica
safety beyond that lease. Client elapsed-time correction in 2048 remains
supported; the speed score therefore still trusts that client input.

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

Live Audio bounds the whole setup/session workload to 30 minutes. Its Redis lease
lasts 31 minutes and carries a unique owner token; cleanup compares ownership
before deleting the key and awaits the bounded release even during cancellation.
Without Redis, the existing active-session set is process-local.

Natal work captures the account's durable generation before queueing or collecting
report input. Report generation and final Telegram delivery use the existing
private-data lease with `require_ltm=False`; natal use does not enable or require
long-term memory. Account erasure can invalidate and drain that work, and an old
queued report cannot adopt a recreated account's generation. The service also
guards direct callers, including the explicitly invoked smoke tool.

## Build, CI and deployment

`pyproject.toml` requires Python `>=3.14,<3.15` and uv 0.13.5; `uv.lock` fixes the
graph. The production Dockerfile uses a digest-pinned Python 3.14 slim image,
locked non-dev dependencies, ffmpeg and a non-root application user.

`.github/workflows/ci.yml` defines the required lint/format/documentation, Mypy,
unit/E2E, serial PostgreSQL/Redis integration, Chromium and offline production
container checks. Independent jobs run in parallel, dependencies use a
lockfile-keyed uv cache, and CI/deploy image builds share a BuildKit cache.
Unit/E2E excludes browser-marked tests, which the Chromium suite requires.
`ci-advisory.yml` runs on the same pushes and PRs and retains production
dependency audit/SBOM/license evidence and private-stack validation. Advisory
failures remain visible without delaying
or blocking application deployment; inventories are uploaded even if the audit
fails. Both CI workflows cancel superseded runs. Deployment serializes transitions
with `cancel-in-progress: false`, accepts successful CI only from a same-repository
push to `vps_testai`, and verifies the source SHA against the current branch tip
before checkout and again before preparing SSH secrets. It builds that exact SHA
and runs migration and health gates. Dependency-only rollback has a restricted
eligibility check; it is not a general code/schema rollback or proof of functional
provider correctness.

The advisory workflow validates the pinned private log-search stack and starts it
to check Grafana health/authentication and Alloy/Loki readiness. Deployment
recreates that independent stack on configuration changes, waits for health, verifies the same
endpoints and synchronizes Grafana's admin password with the persisted secret
before replacing the bot. Health/auth probes are distinct from ingestion:
the CI observability job additionally sends synthetic Docker stdout events
through Alloy/Loki and checks cardinality, filtering, privacy and query limits
through the authenticated Grafana datasource proxy. These disposable-stack
checks do not establish ingestion on a deployed VPS.

The VPS workflow defines `tg-api`, `tg-bot` and `tg-media-cleanup`; database/Redis
are supplied separately. The root Compose file is a legacy single-bot local
alternative, not this production stack. The separate log-search project and its
privacy/retention boundaries are in [the runbook](../ops/observability/README.md).
See [README operations](../README.md#run)
and [dependency maintenance](../README.md#dependency-maintenance).

For exact commands, test-service safety and local hook installation, use
[CONTRIBUTING.md](../CONTRIBUTING.md). Historical pass counts are not current CI evidence.
