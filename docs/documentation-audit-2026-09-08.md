# Documentation audit — 2026-09-08

## Scope and evidence

Baseline: checkout `8fc19516` (`vps_testai`). The audit covers root guidance/reference
documents, current topic guides, both ADRs, historical specs/plans, changelog and
tracked `.jules/` journals. Current claims were compared with manifests, effective
config loading, application boundaries, migration/test helpers and workflow source.
Historical bodies were classified rather than rewritten as if they were current.

In the initial prose-only phase, no runtime code, dependencies, SQL, workflows,
global Codex configuration or installed skills were changed. The separately
requested tooling/CI follow-up is recorded below. No secrets, production database or live provider
calls were used. GitHub queue and VPS status were not fetched; deployment success
claims retained in old reports are explicitly historical.

## Official GPT-6 Astra guidance

Fetched the official [GPT-6 Astra model page](https://developers.openai.com/api/docs/models/gpt-6-astra),
[prompting guidance](https://developers.openai.com/api/docs/guides/latest-model),
and [AGENTS.md discovery guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
on 2026-09-08. They do not define a special Astra-required AGENTS.md schema.

The revised agreements explicitly cover in-scope follow-through, instruction
conflicts, concise reporting, bounded authorized delegation and risk-proportionate
verification. AGENTS.md is the shared source; GEMINI.md points to it. This is a
repository policy adaptation, not a bot model migration or a claim of model certification.

## Findings and corrections

| Prior claim/problem | Evidence and correction |
| --- | --- |
| AGENTS.md only explained encoding; GEMINI.md duplicated a much larger rulebook | Unified instructions with scope, commands, owners and privacy boundaries; replaced Gemini duplicate with pointer |
| Python 3.12+, compatibility target 3.11 | `pyproject.toml`, Dockerfile and CI require Python 3.14; uv is pinned to 0.12.6 |
| Pydantic Settings automatically loads configuration | `Settings` is a BaseModel; `load_settings()` reads environment explicitly |
| Copy `.env.example`; plain `python bot.py` loads it | No `.env.example` exists in this checkout; documented explicit env-file loading |
| `ADMIN_ID` required at load time | Effective loader defaults to 0; usable admin operations still require a configured ID |
| Stale Opencode/image defaults and `USE_OPENROUTER` | Corrected from `load_settings()`; provider choice has four values; removed undocumented legacy switch |
| Hard-coded model catalogs/quotas prove provider availability | Configured IDs and local budgets are not external entitlement or price guarantees |
| Webhook path contains the Telegram token | `bot.py` derives a hash and optionally validates a secret header; user-scoped processor and webhook dedupe are distinct |
| Redis-distributed UserState | `app/state.py` is local LRU/locks with PostgreSQL persistence; distributed primitives have separate owners |
| `memory_manager.py` retrieves LTM | It monitors process memory; LTM is in repositories |
| `app/streaming.py`, `BaseProvider`, universal image/audio class hierarchy | Removed stale/invented map; documented typed delivery and specialized interfaces |
| All calls must use chat router, all keyboards one module, no direct tasks or stdlib JSON anywhere | Replaced blanket rules with ownership-based boundaries and documented existing exceptions |
| AES-GCM key encryption | `app/crypto.py` uses Fernet derived from ADMIN_SECRET |
| Group social graph implicitly captures private memory | Current private-memory consent/epoch and group-scope behavior supersedes the old feature description |
| Telegraph is unconditional/permanent cold storage | Renderer gates both fallback and mirroring on opt-in; ADR and glossary now agree |
| RLS/deletion implies complete privacy or legal compliance | Documented single-DSN/privileged-role limits and external-copy boundary |
| Natal has no production astronomy dependency | Calculator imports `ephem`; geonamescache/tzdata are runtime dependencies; local code owns angles/equal houses |
| Old natal install, benchmark and VPS reports are current proof | Clearly labeled historical; current rollout must revalidate, and smoke writes a report for an existing user |
| All integration safety comes from rollback fixtures | Tests include destructive schema paths and top-level integration markers; document disposable targets and DSN distinction |
| Migration `--check` is a fully read-only drift audit | It may create the tracking table and checks pending versions, not full schema/checksum equality |
| Deployment cancels superseded transitions | Workflow sets `cancel-in-progress: false`; rollback eligibility is limited to verified dependency-only scope |
| Legacy Compose health settings control VPS deployment | Distinguished Compose, Dockerfile and workflow deployment |
| Module sizes, latency improvements, traffic/PR/test counts describe current state | Removed volatile counts and unsupported performance promises from current references; retained historical records with labels |
| Old plan status/required-agent instructions can be read as a new task | Added archival notices to all 20 pre-audit specs/plans and three tracked journals |

## Why these changes

The previous documents mixed design intent, implementation history, operational
claims and agent imperatives. A more obedient model can follow those stale
imperatives literally: add a nonexistent abstraction, rerun an old plan, over-test
prose, bypass a specialized provider path or treat a public mirror as private.
The correction is not more imperative text everywhere: it is one concise rule
source, ownership-based references, explicit history and qualified evidence.

ADR 0002's caller-owned transaction/provenance rationale remains valid. ADR 0001's
single-owner delivery rationale remains valid with the publication opt-in clarified.
The README retains operator configuration and dependency/deployment guidance;
repeated feature histories and universal safety/performance claims were removed.

## Follow-up fixes requested by the user

The original prose-only audit left tooling and confirmed historical corruption
for a separate change. The user subsequently requested those fixes, also on
2026-09-08; the following supersedes the original outstanding-issues list.

- Pre-commit now pins Ruff 0.15.2, matching the lock/CI tool, and registers the
  read-only `documentation-encoding` hook. The local hook was installed with
  `uv run --locked pre-commit install`; other clones must install it themselves.
- `scripts/check_encoding.py` now strictly decodes all tracked/non-ignored new
  Markdown, or explicit filenames from pre-commit. It reports C0/C1 controls
  and the previous known mojibake signatures without rewriting files, survives
  ASCII terminals and Unicode paths, and fails if Git discovery fails.
  The CI lint job now runs this guard.
- Restored 26 confirmed escape-damaged characters in historical CHANGELOG/Bolt
  text: bell replaced the initial `a` in paths/async identifiers, backspace
  replaced `b` in `bot.py`/`big-pickle`, and form feed replaced `f` in `for`.
  This correction does not revalidate historical performance or outcome claims.
- The local standalone uv installation was updated from 0.10.6 to the project's
  required 0.12.6 with `uv self update 0.12.6`. Locked execution synchronized the
  development environment; neither `pyproject.toml` nor `uv.lock` changed.

The ignored machine/plugin skills and user-owned temporary directories remain
untouched. They are not confirmed repository defects and are not targets for
blanket edits/deletion. The current agreements still require disclosure of any
skill conflict. No production database/provider/deployment operation was performed.

## Follow-up verification

- TDD: the new CLI regression suite failed in 10 cases against the old guard;
  all 11 tests pass with the replacement. Cases include malformed UTF-8, controls,
  valid emoji/Cyrillic, known mojibake, missing files, ASCII output, default Git
  discovery, new/ignored documents and discovery failure.
- `uv run --locked pytest tests/test_check_encoding.py --override-ini="addopts=" --timeout=30 -q`: 11 passed.
- Locked Ruff lint and format checks on both changed Python files passed.
- The combined guard and dependency-metadata suite passed: 17 tests. Mypy reported
  no issues in `scripts/check_encoding.py` (only the unrelated unused override note).
- `uv run --locked pre-commit validate-config` and the actual
  `documentation-encoding --all-files` hook passed.
- The actual pinned `ruff-check` hook passed for the two changed Python files.
- `python scripts/check_encoding.py`: passed for 47 Markdown files in this working
  tree (includes six non-ignored local Markdown files beyond the 41 audited docs).
- `uv --version`: 0.12.6; `uv lock --check`: passed.
- `git diff --check`: passed. Application runtime code and dependency manifests
  remain unchanged; follow-up implementation is tooling/tests/CI plus docs.

## Original documentation-audit verification (before follow-up)

- `python scripts/check_encoding.py`: exit 0 before/after the edits.
- Strict UTF-8 decode and local Markdown-link target inspection of 41 documents:
  all decode successfully; no missing local link targets. Existing control characters
  remain only in historical `.jules/bolt.md` (8) and CHANGELOG.md (18), not current guides.
- Static AST/TOML inspection without importing the application: Python range and
  uv pin match the manifest; locked Ruff matches the documented 0.15.2; all 10
  documented `_load_single_model` role defaults match effective `load_settings()`.
- Concrete current-document source paths checked against the filesystem; intentionally
  identified removed `app/streaming.py` is not treated as an active path.
- `git diff --check`: exit 0. Tracked modifications are Markdown only; three new
  Markdown files are the index, this report and the audit plan.

These results describe the initial prose audit. The subsequent tooling tests and
uv update are recorded above. No full runtime suite, paid canary, migration or
deployment was run in either phase.
