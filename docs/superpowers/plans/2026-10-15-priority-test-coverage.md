# Priority test coverage follow-up implementation plan

> **For agentic workers:** Execute the independent assignments below in the current session. Keep each task's files separate and use a fresh read-only reviewer after implementation.

**Goal:** Add meaningful regression coverage for the highest-risk remaining memory, request-admission and response-delivery contracts.

**Architecture:** Exercise current public owners with deterministic event-controlled concurrency. Replace external I/O at its boundary. Correct a runtime defect only after a failing test demonstrates a violation of an existing contract.

**Tech Stack:** Python 3.14, locked uv 0.12.6, pytest/pytest-asyncio, coverage.py, Ruff.

**Spec:** The user's request to continue priority coverage; [repository authority](../../../AGENTS.md), [standards](../../../CODING_STANDARDS.md), [verification](../../../CONTRIBUTING.md), [delivery ADR](../../adr/0001-single-owner-ai-response-delivery.md), [memory ADR](../../adr/0002-provenance-safe-memory-graph-writes.md). Earlier coverage reports are historical inputs, checked against current code and tests.

## Global constraints

- Preserve unrelated work; no commits, pushes, PRs, deployment or migrations.
- Use synthetic inputs; do not read `.env`, real credentials, private logs or birth data.
- Preserve dependencies, shared fixtures and historical evidence.
- All subagents use `gpt-6.1-sol`; use `high` for bounded delivery/admission work and `xhigh` for memory cancellation ownership.
- Run focused offline baselines before changes, then affected checks and a full non-integration run with isolated coverage output.
- Report SQL/Redis/browser/live-service limits separately from offline Python evidence.

## Review focus

- Duplicate graph relations supported by different sources must retain each source's own weight and core status.
- Cancellation of a memory-erasure caller must await already-started child cleanup and preserve the first cancellation reason.
- A Telegram placeholder failure before worker submission must release the user's busy reservation.
- A stored Long Read whose link is invisible must continue the delivery fallback chain without a false receipt or premature mirror.
- Failed or cancelled owners must not affect another user or a newer request; positive retry/control cases must prove continued admission.

## Task 1: Per-source graph attributes

**Files:** `tests/test_memory_graph_source_attributes.py`; minimal correction, if reproduced, in `app/repos/memory_graph_writer.py`.

- [x] Run the existing graph-writer tests as an offline baseline.
- [x] Add exact-edge, semantic-alias and shared-source cases through real `write_graph`; assert literal per-source snapshots and the aggregate edge projection independently.
- [x] Reproduce any contamination before correcting it; preserve tenant/connection/transaction ownership and existing aggregate semantics.
- [x] Run new and neighboring graph tests; record that SQL trigger execution requires separate disposable PostgreSQL evidence.

## Task 2: User-memory cancellation ownership

**Files:** `tests/test_memory_user_cancellation_ownership.py`; minimal correction, if reproduced, in `app/repos/memory_autosave.py`.

- [x] Run current consent and shutdown-ownership tests as an offline baseline.
- [x] Gate registered child cleanup with events; cover ordinary drain, caller cancellation, repeated cancellation and child cleanup failure.
- [x] Assert cleanup completes before caller exit, first cancellation reason survives, registries clear and unrelated-user work remains live.
- [x] Reproduce before correcting; run the new module and neighboring memory task tests.

## Task 3: Request admission failures

**Files:** `tests/test_message_admission_failures.py`; minimal correction, if reproduced, in `app/handlers/messages.py`.

- [x] Run current request-concurrency tests as an offline baseline.
- [x] Exercise real handler admission with Telegram placeholder error and event-controlled cancellation; cover voice and other branches sharing the same reservation pattern where feasible.
- [x] Assert no worker/provider starts for failed admission, no busy reservation leaks, the next valid request is admitted and cancellation keeps its reason.
- [x] Reproduce before correcting; run new and neighboring request/voice tests. Delayed edited-request supersession remains a separately identified follow-up.

## Task 4: Long Read visibility

**Files:** `tests/test_long_read_visibility_contracts.py`; runtime correction only if a new regression proves it necessary.

- [x] Run current renderer tests as an offline baseline.
- [x] Cover Reader-link failure to split, Reader-link failure to Telegraph, Telegraph-link failure to split and cancellation while displaying a Reader link.
- [x] Assert actual visible text/message IDs, receipt transport, final actions, no premature cold mirror and no fallback after cancellation.
- [x] Run new and neighboring renderer/delivery tests.

## Final verification

- [x] Read-only independent review; address Critical/Important findings and rerun affected tests.
- [x] Run locked Ruff/format, type checking when runtime changes require it, full offline non-integration coverage and documentation/diff checks.
- [x] Save compact dated evidence with exact new nodeids, outcomes, source hashes and measured coverage; distinguish current results from historical baselines.
