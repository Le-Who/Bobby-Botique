# Documentation Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Completed documentation audit, 2026-09-08. Retained as an execution
record, not an instruction to rerun or delegate this work. See
[audit findings](../../documentation-audit-2026-09-08.md) for outcomes and limits.

**Goal:** Reconcile project-state documentation and agent guidance with the checkout and official GPT-6 Astra guidance.

**Architecture:** Keep AGENTS.md as the concise repository instruction source, GEMINI.md as a pointer, and topic documents as factual references. Preserve historical decisions and evidence as history rather than silently treating them as current runtime contracts.

**Tech Stack:** Markdown; Python 3.14; committed Python/configuration/SQL/workflow sources.

**Spec:** User request of 2026-09-08: audit all documentation affecting project understanding, including rationale, with AGENTS.md first and official GPT-6 Astra documentation checked.

## Global Constraints

- Documentation-only change: do not change runtime behavior, model defaults, dependencies, deployment, or global agent settings.
- Preserve UTF-8 and user-owned `.pytest_tmp_dependencies/` and `.pytest_tmp_dependencies_focused/`.
- Run `python scripts/check_encoding.py` before and after documentation edits.
- Do not use production credentials, run migrations, generate paid content, or claim live checks passed.
- Execute within this requested documentation task; no commit, push, or deployment.

### Task 1: Establish evidence

- [x] Fetch official GPT-6 Astra prompting guidance and AGENTS.md discovery documentation.
- [x] Inventory tracked Markdown and instruction surfaces; inspect manifests, lifecycle, state, providers, delivery, memory, natal, test fixtures, and CI/deployment source.
- [x] Record concrete mismatches and distinguish current docs from historical plans/results.

### Task 2: Reconcile current guidance

**Files:** `AGENTS.md`, `GEMINI.md`, `README.md`, `CONTRIBUTING.md`, `CONTEXT.md`, `SECURITY.md`, `ROADMAP.md`, `docs/ARCHITECTURE.md`, natal/operations docs, `docs/adr/0001-single-owner-ai-response-delivery.md`.

- [x] Replace duplicated/invented agent invariants with verified boundaries and scoped workflow rules.
- [x] Correct architecture, configuration, setup, privacy, dependency, and verification descriptions.
- [x] Label historical queue, plans, specs, journals, and deployment evidence; preserve original historical bodies.
- [x] Add documentation index and evidence/rationale audit report; add a changelog entry.

### Task 3: Verify the documentation change

- [x] Run encoding protection and strict UTF-8/control-character inspection of current documents.
- [x] Check current-document local paths/links and documented defaults against source.
- [x] Run `git diff --check`; inspect final diff and confirm only documentation changed.
- [x] Record checks, unresolved tooling drift, and unrun live validation in the audit report.

Runtime tests are not a gate for this prose-only change; configuration is inspected without importing the application or loading secrets.
