# Message ownership follow-up implementation plan

> **For agentic workers:** Execute the cohesive handler/state assignment below, followed by read-only task and final reviews. The user has requested execution in this session.

**Goal:** Close the edited-request cleanup and pre-placeholder direct-intent cancellation gaps identified in the previous priority coverage round.

**Architecture:** Keep the real message handlers, canonical user lock, task registry and heartbeat lifecycle under test. Use deterministic events for delayed cleanup and synthetic external boundaries. Preserve existing request routing and delivery policy.

**Tech Stack:** Python 3.14, locked uv 0.12.6, pytest/pytest-asyncio, coverage.py and Ruff.

**Spec:** The user's request to continue these two gaps, [repository instructions](../../../AGENTS.md), [standards](../../../CODING_STANDARDS.md), [contribution checks](../../../CONTRIBUTING.md) and [delivery ADR](../../adr/0001-single-owner-ai-response-delivery.md).

## Global constraints

- Preserve the preceding round's uncommitted code/tests and dated evidence; record the current working files as the baseline rather than resetting to HEAD.
- No commits, pushes, PRs, deployment, migrations, dependency updates or live service calls.
- All subagents use `gpt-6.1-sol`; use `xhigh` for the cohesive ownership implementation/review and `high` for bounded direct-intent contract investigation.
- Use synthetic data, explicit UTF-8 I/O and the existing offline recorder with a unique output directory for each run; do not read `.env` or private logs.
- One source implementer owns the overlapping handler changes; other agents are read-only until its files are frozen.
- Add a genuine failing regression before correcting each existing contract; keep positive controls and clean up every test-owned task.
- Freeze code and run lint/type checks before the final whole offline suite. Run heavy verification sequentially to avoid concurrent collection load on the known 25-second CI-discovery test.

## Review focus

- Delayed cleanup from a superseded ordinary or edited worker must preserve the replacement task's registry/busy ownership and cancellation reachability.
- Reusing the same Telegram message must not let the old heartbeat/error cleanup disable or overwrite the new request's state.
- Edited input must remain request-scoped and must not leak through shared `context.user_data` after success, cancellation or error.
- Cancellation while direct intent is awaiting I/O must release the busy reservation, finish owned cleanup and propagate its original reason; no placeholder/provider work follows cancellation.
- Healthy handled, enriched/unhandled, ordinary fallthrough and error fallback direct-intent outcomes must retain their existing behavior and permit subsequent admission.

## Task 1: Message request ownership

**Runtime scope:** `app/handlers/messages.py`, `app/state.py`; `app/utils/heartbeat.py` or `app/handlers/agent.py` only if a reproduced ownership failure requires a minimal related correction. Preserve compatibility for existing state/heartbeat callers.

**New tests:** `tests/test_edited_request_ownership.py`, `tests/test_direct_intent_admission_ownership.py`. Keep shared fixtures and previous test modules unchanged unless a demonstrated regression requires a small compatibility adjustment approved by the controller.

**Edited-request acceptance**

- [x] Baseline existing request/admission/retry tests before source edits.
- [x] Exercise real ordinary-to-edit and edit-to-edit replacement with an old worker's async cleanup gated by events; show the existing failure before fixing it.
- [x] Verify the replacement remains registered/busy/cancellable, old cleanup cannot erase its ownership, the canonical lock prevents overlapping workload and final cleanup admits a new request.
- [x] Verify shared-placeholder heartbeat ownership and late old-worker error effects where the chosen correction can expose those boundaries.
- [x] Cover edited input success/cancellation/error and prove no stale input reaches a subsequent ordinary request. Prefer the existing explicit per-call override interface when appropriate.

**Direct-intent acceptance**

- [x] Gate direct-intent I/O before placeholder creation, cancel the real handler and reproduce the leaked reservation.
- [x] Verify async cleanup finishes, cancellation reason survives, no placeholder/provider is started and the next request is admitted.
- [x] Cover repeated caller cancellation if cleanup itself suspends, plus handled success, enriched context, ordinary fallthrough and exception fallback controls that are relevant to the fix.

**Integration and verification**

- [x] Run focused new/neighbor tests with scoped branch coverage; run locked Ruff and Mypy as appropriate.
- [x] Complete an independent task review with spec and quality verdicts, addressing significant findings before the whole run.
- [x] Freeze runtime/tests and pass the full offline non-integration suite; record every actual outcome, including any CI-discovery timing failure separately.
- [x] Complete final read-only review, save compact evidence and a dated report, then check UTF-8, links and diff integrity.
