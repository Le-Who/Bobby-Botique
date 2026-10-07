# Coding standards

This is the authoritative home for repository implementation and specialized safety
contracts. [AGENTS.md](AGENTS.md) routes work to the affected sections; setup and
verification recipes belong to [CONTRIBUTING.md](CONTRIBUTING.md).

## Commands and runtime controls

`bot.py` owns lifecycle/registration; `app/bot_commands.py` owns public menu/help
identities, not handler execution. Keep RU/EN descriptions in `app/i18n.py`.
Live command aliases come from handler registrations.

When adding, moving or removing model consumers, prompts or command inputs, keep
the dashboard inventory current in the same change. Prompt definitions use
module-level `register_controlled_text`; their modules and literal consumers are
discovered automatically. Maintain process capabilities and execution notes in
`app/process_policies.py` and `app/runtime_settings/process_evidence.py`; remove
retired registrations with their consumers. Verify inventory coverage with
`tests/test_source_inventory.py` and `tests/test_command_inventory.py`. See
[runtime controls](docs/runtime-controls.md) for dynamic-reader limits and retained
historical overrides.

## Providers, models and configuration

Routed chat/search uses `app/providers/router.py` and typed requests/events. Agentic
research in `app/core/agentic.py` calls Gemini directly; its handler owns model/key
fallback. Explicit Crocodile selections use `google-genai` directly within the game
boundary; image, embedding and Live/TTS paths have their own contracts. Preserve
these specialized integrations rather than forcing every SDK call through the router.

Preserve env/default versus admin-override precedence and intentional `none` lists:
model role defaults and selector parsing live in `app/config.py`; explicit model
catalog overrides live in `app/repos/models_repo.py`. Do not change application
model defaults to match the coding agent's model.

Verify configuration at its actual reader (`load_settings()` or infrastructure
module), not just a Settings field or README table. Check workflow forwarding
separately: adding an environment name/GitHub Secret does not wire it through to
a running container. Reload does not recreate every startup-owned resource. See
[runtime controls](docs/runtime-controls.md) for runtime override semantics.

## Telegram delivery and async work

`app/response_delivery/` owns streamed/completed Telegram response finalization.
Preserve final publication/action rows; handler edits must not overwrite them.
Keep the single owner and typed terminal outcomes; do not reintroduce
`app/streaming.py` or string/tuple terminal states. See
[ADR 0001](docs/adr/0001-single-owner-ai-response-delivery.md).

Reuse `app/utils/text_format.py`, `keyboards.py` and `messaging.py` where their
contracts fit; domain-specific keyboards are valid. Use runtime JSON compatibility
helpers in `app/utils/json_compat.py` where needed; stdlib JSON in tools/tests is valid.

Keep blocking I/O off async request paths. Use tracked background helpers in
`app/utils/background_tasks.py` for detached work. Request-scoped tasks may be
created directly when owned, cancelled and awaited on every exit path.

## State and memory

`app/state.py` is process-local LRU state with PostgreSQL persistence and local
locks. Preserve lock ownership and persistence markers; Redis semaphores do not
make arbitrary process state replica-safe or turn UserState into distributed state.

Private user memory (`app/repos/memory*.py`) is separate from process memory
monitoring (`app/memory_manager.py`). Preserve durable consent epochs/leases,
private-chat scope, deletion and live provenance. Never capture group messages into
private LTM implicitly.

Extraction/consolidation prepare external results before a write transaction.
`memory_graph_writer.write_graph` uses the caller's connection, transaction, tenant
context and source IDs; no hidden pool acquisition or provider calls. See
[ADR 0002](docs/adr/0002-provenance-safe-memory-graph-writes.md).

## Database and migrations

Numbered SQL is under `scripts/migrations/`; manifest validation is in
`app/db/migration_manifest.py`. Add a unique valid migration name and update
schema/RLS checks when appropriate. Preserve startup failure on migration, schema
or RLS errors.

Migration commands are state-changing diagnostics: even `scripts/migrate.py --check`
can create the tracking table. Read [database and integration safety](CONTRIBUTING.md#database-and-integration-safety)
before running migration or database checks; it covers target selection and
destructive integration-test isolation.

RLS is defense-in-depth with the current single migration/runtime DSN: privileged
roles can bypass it. Do not claim hard tenant isolation.

## Daily preparation

`app/games/daily_preparation.py` separates read-only readiness from managed
preparation. Preserve Easy/Hard progress and Redis lease ownership/local fallback.
Explicit admin preparation bypasses the automatic image quota; ordinary automatic
generation does not. Missing art can still allow text delivery; it is not full readiness.
Use the [Daily Crocodile guide](docs/pollinations-daily-croc.md) for the affected
preparation, image and quota contracts.

## Credentials, authentication and webhooks

API keys use Fernet derived from `ADMIN_SECRET` (`app/crypto.py`); changing the secret
can make existing keys unreadable. Telegram Mini App and web/admin guards are
separate boundaries.

Preserve webhook token hashing, optional secret-header validation and deduplication.
`app/webhook_security.py` validates configured secrets and uses constant-time
comparison; malformed inbound headers must not cause an unhandled exception.

## Logging and private observability

`app/observability/` owns correlated events, content policy and bounded output.
Preserve terminal-event ownership and distinguish applied graph writes from committed
transactions. Use the existing pipeline rather than a second log sink. Consult the
[logging policy](docs/logging.md) and [event catalog](docs/log-events.md) for event
and privacy contracts; incident export or collector changes also require the
[private log search guide](ops/observability/README.md).

The protected logging default is `LOG_CONTENT_MODE=full`: authorized message/provider
text is scrubbed and bounded; `metadata` disables text, and `preview` requires
diagnostic scope. Preserve content-forbidden categories in every mode, including
birth data and private-memory bodies. Selected provider credentials are represented
by their last four characters and a non-reversible fingerprint. Never log a complete
key, token, password, header or DSN.

Treat message-bearing logs as sensitive: keep access restricted, bound each event and
retention window, and do not copy them into public artifacts, CI output or alerts.
Grafana datasource access exposes the protected log stream; UI field hiding is not
access control. Local log rotation and single-host Loki retention are not backups
against host loss.

## Public publication

Telegraph publishing is public and opt-in via `TELEGRAPH_PUBLICATION_ENABLED`
(default false), including Reader cold storage and natal mirrors. Keep that gate.
Do not claim universal erasure of already published third-party copies. See
[ADR 0001](docs/adr/0001-single-owner-ai-response-delivery.md) for delivery/publication
ownership and [natal readiness](docs/natal-chart-product-readiness.md) for report work.

## Deployment and readiness

`.github/workflows/deploy.yml` owns VPS deployment; root `docker-compose.yml` is a
legacy local alternative. `ops/observability/` is an independent private stack.
Service readiness/auth checks do not establish end-to-end log ingestion; `/health`
does not establish Telegram/provider availability. Local unit checks do not establish
live Telegram/provider/VPS behavior. Use [README.md](README.md) for operations and
the [private log search guide](ops/observability/README.md) for that stack.
