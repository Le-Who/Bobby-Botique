# Webhook admission ownership — bounded fix

**Goal:** prevent an HTTP request cancellation after a Redis claim from leaving an update unqueued and treated as a duplicate on retry during the claim TTL.

**Scope:** keep current dedupe keys, Redis NX semantics, local fallback, secret validation and queue capacity. Complete the existing claim-to-queue operation under its owner; do not delete Redis claims or introduce a durable queue.

**Base:** the already verified, uncommitted round-2 working tree. Preserve prior code, tests and dated evidence.

## Task 1 — owned admission and lifecycle

**Files:** new `app/webhook_admission.py`, `bot.py`, new `tests/test_webhook_admission.py`, new `tests/e2e/test_webhook_admission_cancellation.py`, new `tests/integration/redis_contracts/test_webhook_admission_ownership.py`.

**Interface:** `WebhookAdmissionGate.admit(operation)`, synchronous `close()` and `WebhookAdmissionGate.shutdown(stop)`; `AdmissionClosed` rejects operations after closing. The operation is the existing capacity-check, Redis/local claim, origin registration and synchronous queue handoff. The gate owns the serialized operation; callers never manage its child task. The narrow ownership helper also covers exceptional cleanup before its first resource await, preserving the existing resource order and avoiding recursive shared shutdown.

- [x] Reproduce cancellation after the first/second persisted Redis claim through the actual registered route. No successful handoff followed by duplicate acknowledgement may be confused with an unqueued update.
- [x] Acquire the admission lock cancellably; recheck closing under that lock. Once the operation starts, shield its owned child and await completion across repeated caller cancellation, then propagate cancellation. Cancellation before acquiring the lock must leave no claim or origin.
- [x] Preserve the first cancellation reason. A finished, cancelled child must not cause a busy loop; observe child errors and preserve diagnostic causes. No request child is detached or left unawaited.
- [x] Close synchronously before the first shutdown await. Drain the same lock barrier and run `application.stop` in one owned shutdown operation, including repeated shutdown cancellation. Waiting/unclaimed requests receive retryable 503; started claims finish handoff before stop. Shutdown is idempotent.
- [x] Wire normal and exceptional lifecycle cleanup through the gate where it has been initialized. Preserve polling behavior and existing startup/stop semantics.
- [x] Regression for repeated cancellation during exceptional cleanup after a registration error, including closure before the blocked alert, admission handoff before PTB stop, first cancellation reason, and cleanup despite an already terminal shared stop.
- [x] Check positive delivery, command identities, duplicate replay, full-queue/no-claim, metadata ownership, timeout/unknown-ACK exception fallback, self-cancel and operation-error cleanup. Real route tests must assert actual queue and origin outcomes, not only mocked call counts.
- [x] Execute real Redis SET/NX with a post-persist acknowledgement barrier, UUID-owned identities and exact cleanup in disposable Redis DB15. Actual gate/queue/origin cancellation and shutdown assertions complement real-route offline tests.
- [x] Run focused gate/webhook/lifecycle/shutdown tests with RED/GREEN evidence, then obtain independent review.

## Task 2 — provider override failure contracts

**Files:** new `tests/test_provider_override_failure_contracts.py`; runtime changes only for a reproduced defect.

- [x] Real Fernet plus a stateful database I/O double: unreadable existing override fails closed even with an environment key; absent override selects the environment key.
- [x] Real settings-repository cache invalidation for set/clear, valid encrypted round-trip, status masking and unsupported provider behavior.
- [x] Real Pollinations generation/transcription entry points do not start HTTP work when the selected override is unreadable.
- [x] Keep current policy when correct; prove test sensitivity to a realistic mutation. Focused checks and independent review.

## Task 3 — final verification

- [x] All delegates use `gpt-6.1-sol`; `high` minimum, `xhigh` for cancellation/lifecycle implementation and review. Disjoint file ownership; no shared fixture edits.
- [x] Synthetic offline runner denies repository `.env` and Python network connections. No live Telegram/provider/VPS access, dependency changes, migration commands, commit or push.
- [x] Full offline unit/E2E, affected real-service tests where needed, locked Ruff/format/mypy, UTF-8, documentation links, registry and diff checks. Missing services are not passing checks.
- [x] Save a separate report and passing evidence; retain historical round-2 snapshots and all unrelated work. Distinguish graceful caller cancellation from a process kill or crash, which cannot be made durable by an in-memory queue.
