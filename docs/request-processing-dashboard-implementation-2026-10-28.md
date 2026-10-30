# Request processing and dashboards: implementation evidence, 2026-10-28

This report accompanies the [original audit](request-processing-dashboard-audit-2026-10-28.md)
and [implementation plan](superpowers/plans/2026-10-28-request-processing-and-dashboards.md).
The user subsequently authorized implementation, commit and push to `vps_testai`.
Work started from `24bdb6e29a878828e170b0a24d94650809696b5d` in an isolated
worktree; unrelated uncommitted work in the original checkout was preserved.
The audit and implementation started on October 28; final local verification
was performed on **2026-10-30 (Europe/Kyiv)**.

## Delivered contracts

| Plan tasks | Implementation and observable result |
| --- | --- |
| R1 | PTB admission counts all unfinished updates, including work already fetched behind per-user locks. Webhook backpressure and polling share the bound. |
| R2 | Local and distributed semaphore acquisition share one monotonic deadline. A lost Redis lease cancels and drains its owner before releasing local capacity; expired leases cannot be revived. |
| R3 | Typed business outcomes separate success, known failure, cancellation and uncertain delivery. A handler result without confirmed settlement is not acknowledged as queue success; document retries distinguish pre-write transients from uncertain writes. |
| R4a–R4d | Versioned local/Redis job protocol with idempotent admission, owner-fenced lifecycle, bounded jobs/bytes/age/retries, workload lanes, cancellation checkpoints, delayed scheduling, retained metadata and restricted manual retry. Operation aliases reconcile coalesced admission. Legacy work is diagnosed and preserved, without automatic replay or deletion. |
| D1–D2 | 2048 rules freeze after first start and edits use revisions. Crocodile generation uses revision/token fences; played content and existing images remain fixed, while ready text can be used before optional image generation finishes. |
| D3–D5 | Daily models/prompts use held settings revisions and per-operation model selection. Tarot separates filling missing results from replacement. Trivia main/super drafts survive sibling saves, conflicts, refreshes and later typing; Crocodile renders untrusted content as text. |
| D6 | Crocodile/Trivia/Tarot long admin actions use shared jobs, preflight, calendar readiness, progress and read-only operation reconciliation. Partial Trivia work validates the preserved sibling before generation and again at execution. Lost/coalesced admission cannot acknowledge an unrelated editor draft. |
| D7 | Broadcast offers use bounded admission and a PostgreSQL delivery journal. Each external attempt has an outcome; retry excludes sent/uncertain delivery. Scheduled delivery and offers have separate accounting, privacy generations and bounded history. |
| O1–O3 | Stage histograms and sample counts, logical requests versus provider attempts, actual lifecycle, independent source freshness, shared refresh work and workload-specific queue controls. Unknown data remains unavailable/stale instead of becoming a misleading zero. |
| O4 | Bounded common HTTP transport, explicit mutation uncertainty, revision/operation reconciliation, latest-read guards, Kyiv calendar defaults, keyboard dialogs/tabs, labels, focus preservation and narrow layouts. |
| O5 | Offline intent-routing corpus and deterministic fixes for quoted/negated instructions and second intent, preserving Ukrainian apostrophes and explicit thinking. No default model or provider pricing/quality claim. |

Current ownership, API and operating details are in [architecture](ARCHITECTURE.md),
[runtime controls](runtime-controls.md), [metrics](metrics-and-dashboard.md),
[Crocodile operations](pollinations-daily-croc.md) and the [event catalog](log-events.md).
Migrations 075–078 add Daily revisions, Tarot generation ownership and the delivery
journal. They must run through the existing deployment/migration gates.

## Review corrections

Independent task reviews and root integration review produced executable
regressions for the following cases, among others:

- Unknown external delivery takes precedence over a concurrent cancellation;
  progress/heartbeat updates do not invalidate an operator's semantic revision.
- Explicit legacy recovery retains malformed and non-replayable effectful work.
- Partial Trivia preparation cannot silently generate an unrequested sibling;
  coalesced or lost admission cannot clear another submitted draft.
- Stale job status, leaderboard reads, queue filters and a pending older selection
  cannot overwrite a newer observation or user selection.
- A mutation 5xx may follow a committed write; the panel preserves uncertainty
  and reconciles instead of automatically repeating the command.
- Confirmed Telegram delivery followed by a bookkeeping failure cannot trigger
  another message; concurrent same-operation admission must have one owner.
- Scheduled horoscope admission and each send recheck the selected slot,
  preferences, local date and previous receipt. Acknowledgment uses the captured
  claim time and cannot move a later marker backward. Daily delivery also
  rechecks the recipient's explicit game choice or current admin default.
- A late admission response after pagehide preserves the operation ID without
  starting new status reads. Reload reconstructs retry relationships from
  server IDs, and journal filters cannot reuse another query's cursor.

## Verification record

Local gates use the locked commands in `CONTRIBUTING.md`, Python 3.14.3,
uv 0.13.5, Node 24.13.0 and the repository's locked Playwright/Chromium.

| Gate | Recorded result |
| --- | --- |
| Complete non-integration, non-browser suite | 6,176 passed, 207 deselected; 278.80 seconds |
| Complete required Chromium suite | 199 passed, 6,433 deselected; 315.90 seconds |
| Configured Mypy, `app bot.py` | No issues in 315 source files |
| Ruff check / format check | All checks passed; 887 files already formatted |
| Documentation encoding / current-guide links | 105 Markdown files; 37 current guides, zero link errors |
| Environment source registry | 112 names, zero drift |
| Local real PostgreSQL/Redis contracts | Skipped because isolated services are absent; the last focused service selection collected and skipped 42 cases |

The configured full type gate passes. An additional exploratory
`--check-untyped-defs` run exposed legacy findings outside the configured gate
(database initialization, metrics/cache collection annotations and an optional
HTTP client); new observation-context annotations were corrected. This extra
mode is not claimed as a passing repository gate.

Earlier broad runs caught outdated fixtures for extracted frontend modules,
actual lifecycle, observed-worker ownership and process-consumer evidence.
The final coroutine-cleanup regression reproduced a stub Task discarding its
completion callback; the fixture now uses a real cancelled Task and asserts
that the nested input closes. Runtime warnings remain errors. The 10,000-update
capacity test disables only INFO/DEBUG logging for its two loggers so unrelated
logging-test setup cannot turn it into a file-writer benchmark; warnings/errors
and the actual processor/queue behavior remain active.

Old scheduler integration tests that expected immediate network sends and
automatic full-message replay were rewritten for short admission and journal
ownership. Real-commit cases now check partial-send quarantine, one admission
across replicas, explicit safe retry, cancellation, marker/commit failure,
subscription changes, pinned timestamps and five concurrent recipients without
database transactions held over network waits. Direct legacy claim-helper
contracts remain covered. These tests require the isolated CI services.

The first [required CI run](https://github.com/Le-Who/Bobby-Botique/actions/runs/40979633235)
passed lint, types, unit/E2E, Chromium and production-image build/smoke. Its
PostgreSQL/Redis job passed 246 cases but exposed an E2E Telegram mock with an
untyped caption and six setup errors referencing the retired Daily scheduler.
The follow-up gives plain-message mocks neutral caption/forwarding metadata and
replaces obsolete preparation-worker tests with real-Redis legacy observation,
expiry and admission-blocking contracts. Current queue fencing remains covered
by the real v2 Lua suite. The focused local follow-up passed 71 cases; six Redis
cases skipped locally and still require the CI services. The separate
[advisory run](https://github.com/Le-Who/Bobby-Botique/actions/runs/40979633199)
passed dependency auditing and the observability-stack checks.

The synthetic admission check executes 10,000 updates through the real PTB
fetcher/processor and verifies at most 100 accepted unfinished updates, then
complete drain. Separate scenarios cover an already admitted second user and
inline request. The monitoring test uses 20 simultaneous readers and checks one
expensive source refresh. These are deterministic contract checks, not measured
production throughput or latency.

## Evidence limits and rollout

- Local browser checks use real Chromium, application templates/CSP and
  synthetic API responses. They establish UI behavior, not external delivery.
- The local Windows host has no isolated PostgreSQL/Redis services. Service
  contracts must run in the existing CI containers; local skips are not passes.
- No live Telegram/provider sends, production database queries, migrations,
  queue cutover, reindex or credential changes were performed as test evidence.
- Redis durability depends on the configured Redis persistence/availability.
  Telegram acknowledgment loss can remain `unknown_delivery`; no exactly-once
  guarantee is asserted.
- Queue defaults are initial bounds. Live TTFT/p95, provider quality, rate-limit
  behavior and cost require a separately scoped canary with operational data.
- Existing legacy backlog requires the explicit drain procedure in runtime
  controls. Do not delete old keys or replay uncertain messages to clear a status.
