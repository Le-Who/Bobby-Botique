# Admission and document outcomes coverage implementation plan

> **For agentic workers:** Use `subagent-driven-development` in this session. The user requested continued implementation; execute the reviewed assignments without another approval handoff. Track acceptance with checkboxes.

**Goal:** Reject voice callback work while an accepted text request owns the user's busy reservation, and distinguish bulk document deletion failure from a genuinely empty result.

**Architecture:** Reuse canonical UserState, the existing voice operation/lock lifecycle, the real document facade/repository and the callback's existing error boundary. Add production-entrypoint regressions, correcting only the two demonstrated local mismatches. Earlier uncommitted work is the baseline.

**Tech Stack:** Locked uv 0.12.6, Python 3.14.3, PTB, pytest/pytest-asyncio, scoped branch coverage, Ruff and Mypy.

**Spec:** The user's request to continue the highest-priority coverage gaps, [agent agreements](../../../AGENTS.md), [standards](../../../CODING_STANDARDS.md), [verification rules](../../../CONTRIBUTING.md), [current architecture](../../ARCHITECTURE.md) and [delivery ADR](../../adr/0001-single-owner-ai-response-delivery.md). Source, actual registered routes and existing contracts establish implementation; historical reports are pointers rather than task authority.

## Global Constraints

- All subagents use `gpt-6.1-sol`; `xhigh` for concurrent admission implementation/review and broad source review, `high` for bounded document diagnosis/implementation/review.
- Preserve every earlier uncommitted change and artifact. Compare this round against captured entry working bytes, not HEAD. No commits, pushes, PRs, deployments, migrations, dependency changes or live services.
- Use explicit UTF-8 I/O, synthetic input and the existing offline recorder with a fresh output directory for each run. Do not read `.env`, private logs or real personal data.
- Use one source implementer at a time. Auditors/reviewers are read-only. Existing test modules and shared fixtures stay unchanged unless a demonstrated compatibility failure receives a local controller-approved adjustment.
- Observe a genuine permanent-test RED before each source correction. Keep positive controls and event-based schedules. Do not suppress warnings, inflate timeouts, detach cleanup or introduce production test hooks.
- Preserve the five-action voice gate, source/message/actor binding, private-data snapshot checks, cancellation behavior, lock ownership and existing provider effects. Do not globally change callback busy policy, task scheduling or text registration.
- Keep document SQL, user scoping, existing retry/reconnect policy, facade signatures and successful/empty counts. Database failure must reach the existing controlled callback error path; cancellation must propagate.
- Freeze source/tests before current global gates and one sequential full offline non-integration suite without global coverage instrumentation. Save before/after hashes. Distinguish diagnostic, RED, exploratory and final GREEN evidence.

## Review Focus

- PTB's real `block=False` text dispatch can return from the outer update processor while admission remains pending. A voice callback must respect the accepted text reservation even with the canonical lock unlocked.
- Busy rejection must preserve pending voice data and the text owner, without beginning provider work, history changes, editing mode, voice operation registration or success UI.
- Voice cancellation, healthy retry, a held canonical lock and another user's independent work must retain their existing behavior.
- Real document database errors must not become empty-result feedback. Failure preserves selected-document state and uses the existing error message; successful deletion retains its exact count and tenant-scoped mutation.
- Cancellation and retry tests establish local coroutine/fake-driver contracts, not a known PostgreSQL commit outcome, durable erasure, distributed state or live Telegram delivery.

## Task 1: Voice callback busy-reservation admission

**Priority:** Confirmed unwanted dual admission/overtaking through actual PTB routing; no corruption, registry loss or cleanup leak was established by the audit.

**Files:** modify `app/handlers/cb_voice.py`; create `tests/test_voice_callback_admission.py`. Other application files and earlier tests remain unchanged unless concrete evidence requires a reported scope extension.

**Consumes:** canonical `state.get_user_state(user_id).is_processing`, `state.get_user_lock(user_id)`, the existing voice pending/operation registry, real PTB registration/dispatch and `UserScopedUpdateProcessor`.

**Produces:** the existing gate for `confirm`, `deep_search`, `transcribe_only`, `retranscribe_flash` and `edit` also respects an existing same-user busy reservation. No new flag, queue, registry or public API is required.

- [x] Baseline the relevant existing admission/voice/processor tests and selected branch coverage before the source change. Inspect fixture isolation before execution.
- [x] Add a permanent regression using real text/callback registration, `Application.process_update`, the real update processor, canonical state and TaskManager. Hold intent I/O after nonblocking text dispatch returns; observe desired busy-rejection RED before changing source.
- [x] Cover all five gated actions during text intent and placeholder I/O. Require busy feedback, the exact pending item and text owner/reservation to survive, and no voice operation, provider/ASR work, history/edit-state mutation or success UI.
- [x] Cover healthy and held-lock controls, post-completion/post-cancellation retry of preserved pending data, another user's work and `voice:cancel` while text remains busy. Drain all owned tasks and callbacks without disturbing the unrelated owner.
- [x] Make the narrow admission predicate correction. Preserve existing atomic unlocked-lock acquisition, privacy checks, replay/operation cleanup and callback cancellation behavior; do not change the outer processor or `block=False` registration.
- [x] Pass new and relevant existing tests with scoped branch coverage and owned lint/format/diff checks. Freeze source/tests, save exact evidence and complete independent spec/quality review.

## Task 2: Bulk document deletion outcome distinction

**Priority:** Confirmed P2 inaccurate empty feedback after exhausted database errors. Existing code preserves selection and never reports a positive deleted count in this failure; do not claim demonstrated lost data or successful erasure.

**Files:** modify `app/documents/repository.py` only where needed; create `tests/test_document_delete_all_outcomes.py`. The facade and existing callback error boundary already support propagation and should retain their behavior.

**Consumes:** real database manager/query, `delete_all_user_documents(user_id)` repository and facade, `document_callback`, real menu generation and UserState. Replace external pool/Telegram/reconnect/persistence effects with synthetic doubles.

**Produces:** successful deletion still returns its count and empty deletion returns zero. Exhausted asyncpg errors propagate to the existing callback error boundary instead of being converted to zero. No new error protocol or SQL/retry policy is required.

- [x] Baseline existing repository/destructive callback cases and selected branch coverage. Inspect all actual application callers and fake-driver isolation.
- [x] Add permanent real-boundary regressions for ordinary `asyncpg.PostgresError` and `asyncpg.InterfaceError`; observe intended RED before the source correction. Require controlled deletion-error feedback rather than empty/success feedback, unchanged selection/other-user state and no misleading menu refresh.
- [x] Preserve positive/empty counts, user-scoped SQL parameters and successful callback state cleanup. Cover successful replay without a second positive deletion message and transient-error recovery under existing retry behavior where relevant.
- [x] Cover cancellation before the fake database mutation: cancellation propagates, no terminal feedback or selection clearing, and acquired resources are released. State the unknown real commit/transport boundary explicitly.
- [x] Make the minimal repository propagation correction, preserving original errors, logging, facade signatures and existing retry/reconnect semantics. Do not redesign single deletion, reads, cleanup jobs or callback UI.
- [x] Pass new and relevant existing tests with scoped branch coverage and owned checks; freeze source/tests and complete independent spec/quality review.

## Final Verification and Artifacts

- [x] Complete broad read-only source review and any bounded fix/re-review before the final full run. Resolve required findings without a new feature or policy change.
- [x] Freeze source/tests; run current global Ruff, format, Mypy and environment-registry gates, then one sequential full offline suite with input hashes before/after.
- [x] Save a new dated report, compact evidence JSON and appendix; add one dated documentation-index row. Preserve older reports and distinguish all actual audit/intermediate outcomes.
- [x] Pass before/after UTF-8 scans, current and explicit dated links, tracked/new-file whitespace and final source/evidence hashes. Complete the same final review seat's artifact addendum and leave work uncommitted.

The audits began on 2026-10-15; the plan is dated when execution was prepared on 2026-10-16. Their existing ignored workspace and immutable entry snapshot are retained. The validated 15-second network-stall threshold/150-ms recheck remains a separate coverage candidate; this plan does not change that policy.

Execution adjustment after the first full run: an existing direct menu mock in `tests/test_callbacks.py` leaked into the new real-menu test. The controller approved a bounded compatibility correction in that existing test, using exception-safe scoped patch restoration. The new document module was restored to its reviewed bytes with every assertion retained. A focused predecessor reproduction, final scoped cohort, same-seat scoped re-review and fresh full run verify the correction; the failed first full result remains evidence. No runtime correction was added.
