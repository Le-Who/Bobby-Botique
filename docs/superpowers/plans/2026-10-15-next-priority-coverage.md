# Next priority coverage implementation plan

> **For agentic workers:** Use subagent-driven-development in this session. The user has requested continued implementation; execute the two reviewed assignments without another approval handoff. Track acceptance with checkboxes.

**Goal:** Protect concurrent private-memory cancellation cleanup and prevent a late long-wait notice from overwriting response content.

**Architecture:** Preserve the existing memory registries, request ownership, heartbeat and single delivery owner. Exercise real production entrypoints with event-controlled external doubles. Correct only demonstrated lifecycle failures; keep the preceding uncommitted work as the baseline.

**Tech Stack:** Locked uv 0.12.6, Python 3.14.3, pytest/pytest-asyncio, coverage.py, Ruff and Mypy.

**Spec:** The user's request to continue the highest-priority coverage gaps, [agent agreements](../../../AGENTS.md), [standards](../../../CODING_STANDARDS.md), [contribution checks](../../../CONTRIBUTING.md), [delivery ADR](../../adr/0001-single-owner-ai-response-delivery.md) and [memory ADR](../../adr/0002-provenance-safe-memory-graph-writes.md). Current source/tests establish runtime contracts; earlier reports are historical evidence.

## Global constraints

- All subagents use `gpt-6.1-sol`; `xhigh` for concurrent ownership implementation/review, `high` for bounded audits or small scoped re-reviews.
- Preserve all previous code, tests, documents and temporary artifacts. Review against entry working bytes, not HEAD. No commits, pushes, PRs, deployments, migrations, dependency changes or live services.
- Use explicit UTF-8 I/O and synthetic inputs. Do not read `.env`, private logs or real personal data. Use the existing offline recorder with a fresh output directory per run.
- One source implementer at a time; auditors/reviewers are read-only. Shared fixtures and earlier test modules stay unchanged unless a demonstrated compatibility issue requires a controller-approved local adjustment.
- Add and observe a genuine regression failure before each runtime correction. Keep healthy controls and deterministic event scheduling. Do not suppress warnings, inflate timeouts, detach cleanup or add production test hooks.
- Freeze all runtime/tests before global lint/type checks and a sequential full offline non-integration run. Do not run full coverage instrumentation in that final run. Keep actual failed/exploratory runs distinct from success.

## Review focus

- Two cancellation owners must join a child's cleanup without sending a second cancellation into its asynchronous finalizer.
- Each cancelled caller retains its own first cancellation reason; unrelated per-user work remains unaffected, while global shutdown still covers its full snapshot.
- An in-flight long-wait edit must be cancelled and joined at the appropriate response handoff; no cooperative notice coroutine may later replace first text, final text/actions or retry UI.
- Ordinary/edited replacement and cancellation before a task's first step must preserve the preceding round's busy, registry, heartbeat and raw-coroutine ownership guarantees.
- Cleanup errors and repeated caller cancellation must not produce unawaited/pending tasks, false successful delivery or new publication/provider effects. Synthetic I/O does not establish remote Telegram ordering.

## Task 1: Concurrent memory cancellation ownership

**Files:** modify `app/repos/memory_autosave.py` only if regression requires it; create `tests/test_memory_concurrent_cancellation_ownership.py`. Existing memory user/shutdown tests remain unchanged.

**Consumes:** `register_memory_task(task, user_id)`, `cancel_user_memory_tasks(user_id)`, `drain_pending_memory_writes(timeout)`, the existing registry and helper. Durable consent epochs remain authoritative and unchanged.

**Produces:** regression protection and minimal idempotent cancellation initiation with an independent awaited join for each owner; no new public API is required.

- [x] Baseline the existing per-user/shutdown ownership tests and record scoped branch coverage.
- [x] Reproduce both user-cancel + user-cancel and user-cancel + zero-timeout drain: child finalizer is held by an Event, both owners remain pending, no second cancellation interrupts it, and registry membership persists until release.
- [x] Cover successful/failing cleanup and repeated cancellation of either joining caller. Preserve each caller's original cancellation arguments and documented result/count, without counting the same child as two tasks.
- [x] Cover single-owner prompt cancellation, already-done children, already-cancelling children, caller self-exclusion and unrelated-user preservation where the operation is per-user.
- [x] Make the narrow correction after RED. Inspect actual cancellation consumers for compatibility; do not add a finalizer deadline, detach cleanup or alter durable-barrier ordering.
- [x] Pass new and relevant existing tests with scoped branch coverage, owned lint/format and independent task review.

## Task 2: Long-wait notice handoff

**Files:** create `tests/test_long_wait_notice_ownership.py`. Runtime candidates are `app/handlers/messages.py`, `app/utils/heartbeat.py`, `app/response_delivery/delivery.py`, `app/response_delivery/coordinator.py` and `app/handlers/cb_ai_actions.py`; touch only the boundaries required by a demonstrated ownership regression. The coordinator's current first-text callback does not cover terminals without text, so a minimal optional lifecycle hook is allowed if needed. No general task-manager or renderer redesign; report any additional required call-site change before editing it.

**Consumes:** current long-wait notice, heartbeat termination event, real TaskManager and the delivery first-text hook. Preserve existing cancel/retry actions and the final delivery owner's text/keyboard.

**Reviewed scope extension:** A genuine independent-review regression reproduced the real agent exception fallback writing recovery before notice retirement. Include the minimal fallback handoff in `app/handlers/agent.py`, with its entry bytes preserved and focused RED/GREEN plus re-review. The five original boundaries alone cannot protect that output path.

**Final-review scope extension:** A real ordinary-chat generation-check refusal reproduced the same obsolete-notice overwrite. Include the necessary direct-output handoffs in `app/handlers/ai_chat.py` and `app/handlers/ai_search.py`, including reachable research/QnA stop/refusal boundaries and Lyria terminal output. Preserve existing progress/long-wait timing, provider/music/research policy and privacy decisions. Do not retire every notice at handler startup. Entry bytes are saved; the original author adds permanent RED/GREEN coverage and returns a scoped diff to the same final reviewer.

**Produces:** event-controlled regressions and an explicit owned handoff for pending/in-flight notices. Existing heartbeat callers remain compatible. Describe the minimal lifecycle/API seam in the report before changing source.

- [x] Baseline existing long-wait/heartbeat/delivery ownership tests and scoped coverage.
- [x] Reproduce a gated notice edit that otherwise commits after first response text or final content. Assert visible content/actions, not only an event or a mocked call count.
- [x] Verify cooperative cancellation cleanup is joined by the appropriate handoff before ownership is released; keep exact guarantees local and avoid claiming cancellation retracts an already-applied remote Telegram edit.
- [x] Cover no notice before threshold, pending notice with real stop/retry action, already-set event, ordinary Telegram errors, caller cancellation/repeated cancellation, and cancellation before notice/worker startup as relevant to the correction.
- [x] Exercise real delivery first-text/final paths and real edited-request handoff where the new seam affects them, preserving busy/registry/heartbeat/lock and final action rows.
- [x] Pass focused/scoped and neighboring tests plus independent task review, fixing significant findings before the broad run.

## Final verification and artifacts

- [x] Freeze runtime/tests, run global Ruff/format/Mypy/environment checks, then one sequential full offline suite with source hashes before/after.
- [x] Complete broad read-only source review and any bounded fix/re-review before final verification claims.
- [x] Save a new dated report, compact evidence JSON and appendix; add one dated documentation-index row and preserve older reports.
- [x] Pass before/after UTF-8 scans, current and explicit dated-document links, diff integrity and final source/evidence hash verification. Leave all work uncommitted.
