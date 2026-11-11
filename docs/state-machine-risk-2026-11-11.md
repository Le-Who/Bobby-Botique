# State-machine risk and remediation

This assessment covers `vps_testai` at `768a36e58d6cb0612b52b74102f2be87f9acacca`
and the existing working-tree changes inspected on 2026-11-11. It extends the
conversation inventory with the canonical chat-history persistence checkpoint.
Three GPT-6.1-sol workers used extra-high reasoning for the privacy/state,
delivery/jobs, and request/provider/voice/game reviews. The coordinator reviewed
browser and runtime state, integrated the patches, and ran the repository checks.
Follow-up implementation and independent same-model
reviews also used GPT-6.1-sol with extra-high reasoning.

The risk column records the initial engineering follow-up priority; the final
column records the local implementation or retained protection. The ranking weighs
irreversible data or external effects, competing actors, cancellation/recovery behavior, existing
ownership safeguards, and the strength of the evidence. Qualitative risk levels
express judgment, without a measured production incident rate. Related entries
share a rank when they protect the same invariant. Every family from the preceding
inventory, including its five explicitly modeled neighbors, appears below.

`Reproduced` means a controlled offline execution used the actual owning function
with synthetic dependencies. `Protected` means reviewed safeguards and existing
regressions reduce the apparent risk. `Source concern` identifies a transition
whose unsafe outcome still needs a discriminating execution. Browser and offline
execution evidence is recorded below. Fixed locally means
implemented in this working tree, without a claim of deployed behavior.

| Rank | Risk | State machines covered | Evidence and disposition |
| --- | --- | --- | --- |
| 1 | High | Reminder polling, AI dispatch, delivered marker, and cancellation | Fixed locally: durable source/slot claims, renewable ownership, per-attempt receipts, typed final AI acknowledgment, bounded no-effect retries and unknown-effect quarantine. Reminder workers join shared shutdown drain. [Dispatch](../app/handlers/cmd_reminders.py), [journal](../app/repos/scheduled_deliveries.py), [regressions](../tests/test_scheduled_delivery_lifecycle.py) |
| 2 | High before patch | Canonical chat-history save checkpoint | Reproduced loss of the unsaved suffix after a failed or rejected write, and speculative success markers before commit acknowledgment. Fixed locally by publishing all three markers after the transaction exits successfully. [Owner](../app/repos/chats.py), [regressions](../tests/test_chat_persistence.py) |
| 3 | High | Scheduled brief subscription, generation, and last-sent marker | Fixed locally: capture subscription revision, UTC slot and privacy epoch, check before each content operation and acknowledgment, and recover same-day safe retries independently of the initial preferred hour. [Owner](../app/handlers/scheduled_briefs.py), [migration](../scripts/migrations/082_scheduled_delivery_ownership.sql) |
| 4 | High | Tarot conversation, reset, idle routing, and session exit | Fixed locally: owned immutable turns, predecessor drain, stale completion fences, hydrated end/idle entry, and explicit mode-switch cancellation. Competing ordinary requests receive a busy response. [Owner](../app/handlers/tarot_chat.py), [regressions](../tests/test_interaction_ownership.py) |
| 5 | High | AI-generated role creation and regeneration | Fixed locally: input/generation/preview identities, token-bound modern callbacks, first-use privacy generations, exact-epoch apply/save, owned cancellation, and ordinary-request admission checks. Role entry leaves Tarot before publishing its input prompt. [Owner](../app/handlers/msg_roles.py), [callbacks](../app/handlers/cb_roles.py) |
| 6 | High impact, protected | Consent/revocation barrier, private-data leases, and account erasure | Durable epochs, advisory-locked acquisition, lease drain, exact-generation compensation, and owned erasure already exist. Preserve these contracts and exercise real PostgreSQL contention, cancellation, and commit boundaries. [Consent](../app/repos/memory_consent.py), [erasure](../app/repos/users.py) |
| 7 | High impact, partial service evidence | Game mutation/replay and classic Crocodile finalization | Fixed locally: renewable token leases, fail-closed configured Redis operations, cancellation-owned children, token-fenced classic cache writes and stale-socket reload. Daily PostgreSQL progress uses optimistic comparison. Cross-store/crash limits remain below. [Runtime](../app/games/crocodile_runtime.py), [regressions](../tests/test_game_mutation_lease.py) |
| 8 | High impact, protected | Request admission/execution and retry/response ownership | Fixed locally: direct intent retains admission through delivery; edits/retries carry source/response identity; heavy callbacks reserve per-user admission before UI/capacity waits and bind their source. Running interactions are drained or rejected before competing work. [Dispatcher](../app/handlers/messages.py), [callbacks](../app/handlers/cb_ai_actions.py), [regressions](../tests/test_edited_request_ownership.py) |
| 9 | High impact, protected | Webhook admission/shutdown, webhook/request deduplication, and outstanding update capacity | Fixed locally: token-owned pending reservations become accepted only after queue handoff. Redis outage, uncertain acknowledgments and pending/legacy duplicates return retryable 503; confirmed accepted duplicates return 200. [Dedupe](../app/webhook_dedupe.py), [admission](../bot.py), [regressions](../tests/test_webhook_dedupe.py) |
| 10 | High impact, protected | Broadcast batch/recipient/attempt journal, managed queue jobs, deferred AI delivery, daily-game subscription/discovery, horoscope slots, and tarot slots | Typed outcomes, owner tokens, revisions, quarantine, privacy epochs, subscription fingerprints, and slot fences already govern effects. Extend the production Redis/PostgreSQL contract evidence before structural changes. [Queue](../app/queue_contracts.py), [store](../app/queue_store.py), [Redis transitions](../app/queue_lua.py), [broadcast](../app/broadcast_jobs.py), [deferred](../app/deferred_response.py) |
| 11 | High impact, protected | Memory extraction/consolidation and temporal graph-edge validity | Prepared mutation plans, live source provenance, transaction rechecks, and source-specific projections protect derived facts. Exercise actual trigger/FK behavior and delete-versus-consolidate races. [Extraction](../app/repos/memory_extraction.py), [consolidation](../app/repos/memory_consolidation.py), [writer](../app/repos/memory_graph_writer.py) |
| 12 | High impact, protected within one process | User-state hydration, persistence, eviction, and retirement | Failed-load propagation, canonical live-state identity, a separate persistence lock, and retry markers already exist. Keep those invariants under eviction/erasure/cancellation tests. [Owner](../app/state.py) |
| 13 | High privacy impact, protected | Natal generation, storage, publication, delivery, and retention | Ingress epochs, private-data leases, stage guards, and opt-in public mirrors exist. Verify durable generation/storage contracts. Report expiry intentionally performs soft deletion. [Generation](../app/natal/service.py), [storage](../app/natal/storage.py), [contract](natal-chart-product-readiness.md) |
| 14 | High impact, protected | Provider selection/racing/fallback, provider-stream consumption, and agentic research budget/loop | Typed terminals, drained losers, first-text ownership, and shared budgets exist. Extend adversarial cancellation/timeout coverage when changing routes or policies. [Router](../app/providers/router.py), [coordinator](../app/response_delivery/coordinator.py), [research](../app/core/agentic.py) |
| 15 | High impact, protected | Progressive Telegram rendering, delivery recovery, and long-read publication/retention | One finalizer owns text/actions and typed outcomes. Keep storage acknowledgment, visible Reader actions, publication consent, and partial sends in the same contract. [Renderer](../app/response_delivery/renderer.py), [delivery](../app/response_delivery/delivery.py) |
| 16 | Moderate-high impact, protected | Voice transcript confirmation, voice-text edit continuation, and TTS queue/synthesis | Exact pending-source/operation identity, privacy epochs, FIFO capacity, and cleanup ownership exist. Preserve stale-callback, rejected-admission, and cancellation regressions. [Callbacks](../app/handlers/cb_voice.py), [TTS](../app/voice_engine.py) |
| 17 | Moderate-high impact, protected | Live Audio server ownership and protocol | Bounded sessions, unique Redis owner tokens, local fallback exclusion, and awaited release exist. Test actual provider disconnect/resumption against the documented service boundary. [Owner](../app/web_miniapp.py), [regressions](../tests/test_live_session_lease.py) |
| 18 | High access impact, bounded state | Dashboard login session | CSRF challenge consumption, session lifetime, authentication guards, and rate limiting already model admission. Preserve expiry/logout/failed-login behavior when changing authentication. [Owner](../app/web.py) |
| 19 | Moderate-high impact, protected | Distributed capacity leases, provider-key health/quota reservations, game AI/Live cooldowns, and circuit breakers | Token ownership, renewal, pending-versus-committed usage, cooldown expiry, and generation-aware probes are explicit safeguards. Verify lost-renewal and lost-ack paths with disposable Redis. [Capacity](../app/adapters/concurrency.py), [keys](../app/repos/keys.py), [quota](../app/providers/tts_quota.py), [circuits](../app/circuit_breaker.py) |
| 20 | Moderate-high impact, protected | Bot startup/shutdown, background tasks, database pool recovery, config/runtime snapshots, and log writer/diagnostic scope | Extended locally: mandatory AI callbacks join the shared manager; inline child cleanup drains through repeated cancellation; lazily registered static prompt baselines remain readable in a pinned runtime revision. Existing ordered shutdown and state persistence ownership remain. [Tasks](../app/utils/background_tasks.py), [prompts](../app/prompt_registry.py) |
| 21 | Moderate, protected | Daily Crocodile preparation/freezing, 2048 authoring, trivia authoring/publication, and tarot preparation | Revisions, generation tokens, first-play freezes, and main/super separation already exist. Preserve stale-generation and admin-versus-player contention cases. [Daily jobs](../app/games/admin_jobs.py), [Crocodile](../app/repos/crocodile_daily.py), [2048](../app/repos/daily_2048.py), [trivia](../app/repos/daily_trivia.py), [tarot](../app/repos/tarot_generation.py) |
| 22 | Moderate, protected | Daily Crocodile player progress, 2048 player/practice progress, and trivia main/super progress | Fixed locally: Daily Crocodile compares expected attempts/hints and 2048 compares expected moves before saving. A conflicting WebSocket action requests reload instead of caching false acceptance. Existing question/revision and replay ownership remains. [Crocodile](../app/repos/crocodile_daily.py), [2048](../app/repos/daily_2048.py) |
| 23 | Moderate | Document interaction/upload confirmation and image-generation interaction | Fixed locally: image execute/regenerate explicitly admit through the shared manager, release exact ownership on rejection/prestart cancellation and drain older interactions under the request lock before generation. Existing awaited document work and file cleanup remain. [Image callbacks](../app/handlers/cb_image.py), [regressions](../tests/test_draw_admission.py) |
| 24 | Moderate | Manual role creation, role-prompt editing, role/conversation renaming, and conversation/branch switching | Fixed locally: context changes retire owned work and pending canvas/rename drafts. Entry intents fence suspended older transitions. Rename failures retain input; branch restoration rewrites equal-length histories. Rejected model/branch writes do not claim success. [Branch](../app/handlers/cb_branches.py), [regressions](../tests/test_context_interaction_transitions.py) |
| 25 | Moderate-low, bounded | Compatibility selection/date collection and explicit natal, horoscope, key-entry, and model-entry ConversationHandlers | Flow expiry and framework state maps provide owners. Keep transitions between modes, edited inputs, cancellation, and auth rechecks covered. [Compatibility](../app/handlers/compatibility.py), [natal](../app/handlers/natal_chart.py), [horoscope](../app/handlers/horoscope_subscription.py), [keys](../app/handlers/cmd_keys.py), [models](../app/handlers/cmd_models.py) |
| 26 | Moderate, protected | Message debounce/forwarded collection, split speech-source collection, and media-album assembly | Exact-slot cleanup, epochs, pending IDs, and child-task drain already exist. Preserve continuation/boundary and cancellation tests. [Debounce/speech](../app/middleware/debounce.py), [albums](../app/handlers/msg_media.py) |
| 27 | Moderate | Inline generation/recovery and collaborative boards | Fixed locally: board summary writes reject closed/changed snapshots, while per-board publication locks order summary and close UI in one process. Inline progress and Tarot standby cleanup drain before finalization. Cross-process Telegram edit ordering remains a limit. [Boards](../app/handlers/board_handler.py), [regressions](../tests/test_presentation_ownership.py) |
| 28 | Moderate, protected | Browser runtime-control drafts, daily-editor drafts, and job submission/operator commands | Revision conflicts, unknown outcomes, operation IDs, and local draft retention already distinguish mutation outcomes. Preserve pending edits and reconcile the submitted operation after interrupted responses. [Controls](../app/static/js/controls.js), [daily jobs](../app/static/js/daily-jobs.js), [broadcast jobs](../app/static/js/daily-broadcast-jobs.js), [queue commands](../app/static/js/controls-queues.js) |
| 29 | Moderate | Browser game connection/action acknowledgments, Live reconnect, and microphone/playback state | Transport recovery and local flags interact with server state. Exercise stale sockets, pending action replay, turn interruption, permission failures, and intentional close. [Crocodile](../app/templates/crocodile.html), [2048](../app/templates/daily_2048.html), [trivia](../app/templates/daily_trivia.html), [Live](../app/templates/live_audio.html) |
| 30 | Moderate-low | Natal/compatibility forms and Mini App settings/undoable deletion | Fixed locally: MiniApp saves retain newer edits, reject duplicate submission, and fence late cache/server responses; delete undo expires when the owned request starts. Backend field patches reload and revalidate model/reasoning under the user lock; consent revocation stays immediate and rejected writes return conflict. [API](../app/web_miniapp.py), [UI](../app/templates/miniapp.html), [browser regressions](../tests/test_miniapp_role_browser.py) |
| 31 | Low-moderate | Daily Telegram prompt/result message lifecycle | Fixed locally: optional Crocodile result/thermometer refreshes join shared task ownership, reject capacity cleanly and cannot restart from cancellation cleanup. Thermometer edits reload active state under the mutation lease and retire before terminal edits. Existing 2048/trivia obsolete-message deactivation remains. [Classic](../app/games/crocodile_telegram.py), [daily](../app/games/crocodile_daily_telegram.py), [regressions](../tests/test_game_refresh_task_lifecycle.py) |
| 32 | Low, observational | Dashboard source-cache freshness and browser live-data freshness | Last-good snapshots, source timestamps, and stale/unavailable states reduce misleading health displays. Verify out-of-order refreshes and SSE reconnection when changing these paths. [Source cache](../app/dashboard_sources.py), [dashboard](../app/templates/dashboard.html) |

The implementation remains in the local working tree. No commit, push or
production deployment was performed. The migration chain was subsequently applied
only to the disposable test cluster documented below. The branch and base above identify the starting
point, not a shipped revision. Existing working-tree changes were preserved.

The main structural change is a small PostgreSQL scheduled-delivery journal that
reuses the existing broadcast delivery owner. Migration 082 adds source/slot
uniqueness, attempt receipts, subscription revisions, retry metadata and admin RLS
policies. Both reminder and brief workers capture the account generation before
provider work. Every content attempt rechecks source and ownership. Placeholder
receipts protect recovery but do not acknowledge a completed AI response. A lost
or partial receipt is quarantined. A confirmed no-effect failure can retry within
a bounded budget. Temporary private-data lease unavailability is retryable when
the source is still current. Helper cleanup retains ownership through repeated
parent cancellation. Optional research TTS keeps its own existing queue, source
identity and privacy epoch after the text claim settles.

Role and Tarot work now has a typed interaction owner on the canonical process
state. Replacement invalidates old work before awaiting its cleanup. Provider
results cannot publish into a replaced preview/session. Modern role buttons carry
the preview token, and writes carry the exact privacy epoch. Legacy callbacks are
accepted only for legacy payloads. Tarot turns use copied history. End and idle
entry hydrate persisted state before mutation, preventing restart-time loss of
unrelated settings. Ordinary requests and explicit context changes coordinate
with these owners. This is process-local ownership, not replication of all
`UserState` fields.

Canvas prompt and role/conversation rename drafts use the same input ownership
boundary. A new explicit input mode retires the preceding local draft; reset can
do so without receiving the Telegram context. Exact-owner cleanup preserves newer
drafts and unrelated canvas/model settings. Rename repositories acknowledge their
updates before success UI or metrics, retaining the input stage after failure.
Entry intents are established before asynchronous feedback/hydration and checked
after transition waits; an older entry cannot retire a newer input, and reset
invalidates suspended entries.
[Input arbitration regressions](../tests/test_text_input_arbitration.py) cover both
directions of mode handoff and reset.

Request and webhook changes preserve bounded admission through the whole owned
operation. An edit can replace its own source/response, rather than an unrelated
newer request. Webhook state distinguishes a pending Redis reservation from a
confirmed local queue handoff. Unknown or pending admission remains retryable.
This does not provide durable ingress across process crashes.
Continuation validates the clicked response, privacy generation and branch before
admission metadata changes, then rechecks after capacity waits. Canonical renderer
receipts plus admitted source lineage preserve split/replacement responses, while
newer direct intents and cleared logical requests invalidate stale buttons.

Game mutation leases renew while work runs. Configured Redis failures do not
admit a second process-local writer. Classic game cache/runtime operations check
the current token atomically, and missing current games cannot be restored from
an old socket copy. Daily durable writes compare expected progress and reject a
stale computation. PostgreSQL comparison is not an atomic fence shared with Redis
for a database command already in flight. Bounded replay and attempt receipts do
not establish exactly-once external effects across crashes.

Optional game refreshes now use shared task-manager admission and exact-owner
cleanup. They cannot resurrect after cancellation. Classic thermometer publication
reloads current game state under mutation ownership and retires before finalization.
Heavy AI callbacks reserve per-user admission before awaited UI or global capacity
and bind to the clicked response's source. Callback workers retain their pinned
runtime settings revision while joining shared shutdown drain.

MiniApp save acknowledgment applies to the submitted draft, preserving newer edits.
Cache and server responses carry load/revision checks. Undo belongs to a specific
pending deletion and stops when the request starts; close/unload flushing uses the
same owner. Backend settings patches reload current history and revalidate model
policy under the user lock. Consent uses its separate immediate durable write.

Board summary publication and close use a per-board weak lock registry inside one
process, without holding a database transaction over Telegram. Durable closed and
entry-count guards protect storage across processes. Telegram edit ordering between
independent replicas is still outside that local publication guarantee.

Lower-priority corrections retain the existing implementation language and domain
boundaries. A language rewrite would not address the demonstrated ownership and
acknowledgment defects. Boards reject stale summaries, branches explicitly
replace their histories, inline children drain before finalization, and settings
writes preserve admitted history while reporting durable rejection accurately.
Rows marked protected retain their existing guards and regression suites rather
than acquiring a second state-machine framework.

The lower-priority audit retained the existing login CSRF/session/rate-limit
contracts; quota reserve/commit/refund and circuit-generation ownership; daily
preparation revision/legacy-lease blockers; form identities and authorization;
exact debounce/album collection ownership; admin operation IDs and draft
reconciliation; browser transport recovery; and dashboard single-flight,
invalidation, last-good snapshots and owned shutdown. Closest retained checks
include [login contracts](../tests/test_admin_auth_contracts.py),
[quota](../tests/test_tts_quota.py),
[circuit ownership](../tests/test_circuit_breaker_ownership.py),
[daily preparation](../tests/test_daily_preparation.py),
[admin jobs](../tests/test_daily_admin_jobs.py),
[debounce](../tests/test_debounce_ownership.py),
[albums](../tests/test_media_group_runtime.py),
[control drafts](../tests/test_controls_ui.py),
[daily job UI](../tests/test_daily_jobs_browser.py),
[queue controls](../tests/test_queue_controls_browser.py),
[source cache](../tests/test_dashboard_source_cache.py) and
[source failures](../tests/test_dashboard_source_failures.py).

The final offline, lint and type runs used the combined local source after the
entry-intent correction. Browser results cover the same final frontend source;
subsequent corrections affected Python ownership boundaries. These are separate
checks; overlapping focused runs are not added to the full-suite counts.

| Check | Result |
| --- | --- |
| Full offline unit/E2E selection | 7,307 passed, 1 skipped, 245 deselected in 256.98 seconds |
| Full Chromium browser selection | 215 passed; controlled network responses, actual browser |
| Final MiniApp Chromium subset | 18 passed; included in the browser coverage above |
| Ruff lint and formatting | Passed; 957 Python files already formatted |
| Mypy | Passed; 325 source files |
| Markdown encoding | Passed; 108 files |
| Current documentation links | Passed; 38 documents, zero errors |
| This dated report's links | Passed; zero errors |
| Environment registry | Passed; 118 names, zero changes |
| Working-tree whitespace check | Passed with repository Git settings |
| Affected PostgreSQL/Redis contracts | 67 passed, zero skips/failures; real PostgreSQL 17 and Redis 7 |
| Full integration selector | 320 passed, 6 collector-only skips, 7,523 deselected in 183.37 seconds; zero failures/errors |
| Broader real Redis regression selection | 87 passed, zero skips/failures; overlaps 16 cases above |
| Migration chain | All 86 files applied; second apply was a no-op; pending check passed |
| Startup database checks | All 71 expected tables and critical columns validated; RLS configured/enabled for all 71 |

The principal final commands were:

```powershell
uv run --locked pytest tests/ --ignore=tests/integration -m 'not integration and not browser' --override-ini="addopts=" --timeout=30 -q --tb=short --show-capture=no
$env:GEMAIBOT_REQUIRE_BROWSER_TESTS='1'
uv run --locked pytest tests/ --ignore=tests/integration -m browser --override-ini="addopts=" --timeout=30 -q --tb=short --show-capture=no
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app bot.py
uv run --locked python -X utf8 scripts/check_encoding.py
uv run --locked python -X utf8 scripts/check_docs_links.py
uv run --locked python -X utf8 scripts/check_env_registry.py
git diff --check
```

The previously skipped service selection was executed on 2026-11-11 after the
user requested disposable services. Its final ownership-checked run passed all 67 cases in 50.01 seconds:
scheduled delivery (26), Daily Crocodile progress (10), Daily 2048 (15), Redis
preparation/game ownership (8), and webhook admission ownership (8). A separate
broader Redis selection passed 87 cases in 13.92 seconds; it overlaps those 16
Redis cases and is not an additional 87 unique tests.

The host lacked Docker and installed WSL. A portable, headless QEMU process runs a
4-GiB RAM-only Alpine 3.22.8 guest. QEMU's installer was obtained from its linked
[Windows binary provider](https://qemu.weilnetz.de/w64/) and verified against the
published SHA512; the Alpine ISO was verified against its published SHA256.
The guest runs PostgreSQL 17.11, upstream
[pgvector v0.8.7](https://github.com/pgvector/pgvector/tree/v0.8.7), and Redis 7.2.11.
Pgvector was built from its verified upstream archive because Alpine 3.22's
packaged 0.6.2 lacks `halfvec`. Redis was explicitly pinned to the signed Alpine
3.21 package `redis=7.2.11-r0`; the Alpine 3.22 default is Redis 8.

Only host loopback ports are forwarded: PostgreSQL at `127.0.0.1:55439` and Redis at
`127.0.0.1:56389`. PostgreSQL data lives inside the disposable guest at
`/var/lib/postgresql/data-u2y81182`; the migrated template is
`gemaibot_contract_template_u2y81182`. `fsync`, `full_page_writes` and
`synchronous_commit` remain enabled. Test fixtures cloned and dropped a separate
database per PostgreSQL case. A further readiness clone passed actual
`validate_schema` and `setup_row_level_security`; it was then dropped. No contract
clones remain. Redis databases 13, 14 and 15 were empty after the broader selection.

A subsequent full integration run selected all 326 cases using `tests/ -m
integration`. It passed 320 cases in 183.37 seconds, with zero failures or errors.
The six skips were exclusively the isolated Grafana/Loki/stdout-collector cases
in `tests/observability/test_collector_e2e.py`; that separate logging stack was not
configured. No PostgreSQL or Redis integration case was skipped. The original 67
contracts are included in the 320 passes, along with 253 additional passing cases.

The full run used a new working database cloned from the pristine migrated
template. PostgreSQL contract fixtures made their further per-case clones from
that working database. After the run, no connections or contract clones remained;
the successful working database was dropped, and the template retained all 86
migration records. Independent post-run checks confirmed the same Redis runtime
and zero keys in databases 13, 14 and 15. No application-code correction was needed.

The full-suite wrapper validates the owned instances, disables dotenv file reads,
creates a unique working clone and pytest artifact directory for each invocation,
and removes a successful working clone after completion. It deliberately leaves
the separate collector stack unconfigured. Its command is:

```powershell
uv run --locked python -X utf8 C:/Users/user/AppData/Local/Temp/gemaibot-contract-services-20261111-u2y81182/run_full_integration.py
```

The audited run artifacts are under `full-runs/57cc726b774a/` in the scratch bundle:
JUnit XML, selector/database metadata, exit code, and PostgreSQL/Redis cleanup
snapshots. An independent GPT-6.1-sol metadata audit confirmed 326 unique cases,
320 passes, zero errors/failures, and exactly six collector skips, including all
67 requested service contracts.

The entire 86-file migration chain was applied to that template, then applied a
second time, followed by `--check`. All three commands exited zero, with 86
tracking records including 082 and no pending migration. No production target or
repository configuration was changed. Actual test invocations used a wrapper
that disables dotenv lookup, asserts the exact owned ports/database, checks the
PostgreSQL data directory and Redis runtime fingerprint, and closes its preflight
connections before cloning begins. No integration safety guard was bypassed.

The service runner, migration logs, version/cleanup snapshots, download manifests,
and an ownership-checked stop helper are stored in this task's local scratch
bundle:

```text
C:/Users/user/AppData/Local/Temp/gemaibot-contract-services-20261111-u2y81182/
```

To rerun the exact 67 cases while these owned services remain active:

```powershell
uv run --locked python -X utf8 C:/Users/user/AppData/Local/Temp/gemaibot-contract-services-20261111-u2y81182/run_contracts.py
```

The wrapper uses serial execution and the repository's 30-second per-case timeout.
The bootstrap HTTP server has been stopped. The isolated database VM is retained
for follow-up verification; its ownership-checked `stop_services.py` stops only
this task's processes and discards the RAM-only databases. Downloaded binaries,
bootstrap sources and evidence logs remain in the scratch bundle. Future setup
must still follow [the repository safety guide](../CONTRIBUTING.md#database-and-integration-safety).

Independent GPT-6.1-sol reviews reproduced additional defects in first-use epochs,
cold hydration, cancellation drain, retry liveness, positive split-message
continuation and competing input entries. The corresponding corrections have
regressions and focused rechecks. The final limited review found no remaining
actionable issue in the corrected entry-supersession/reset path.

The real-service cases exercise the covered SQL ownership/locking, RLS, progress
comparison and Redis Lua/lease contracts. They do not establish behavior outside
that selection. Production image building, live provider/Telegram outcomes and
deployed shutdown were not exercised. No formal model checker was run. The local ingress,
process-state, board publication and cross-store boundaries described above remain
limits of the implementation, rather than exactly-once guarantees.
