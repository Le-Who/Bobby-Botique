# AGENTS.md — Repository working agreements

Applies to this repository. Last reviewed: 2026-09-08, against checkout
`8fc19516`. This is coding-agent guidance, not the bot's system prompt.

## Scope and working style

- Follow the user's requested outcome through implementation and appropriate verification.
  For small, reversible choices inside that scope, state useful assumptions and proceed.
- A request to explain, review, or diagnose is not permission to implement, publish,
  deploy, migrate a database, rotate keys, or change external state.
- Ask only when a missing decision materially changes the result or requires new authority.
  A plan is a working aid, not a reason to leave an authorized change unfinished.
- System/developer instructions and explicit user instructions take precedence over
  repository conventions and skill guidance. Read applicable nested instructions.
  If a skill blocks or redirects work, identify its file and relevant requirement,
  distinguish that requirement from your interpretation, and explain the impact.
- Use subagents only when the user or governing instructions authorize them. Give
  authorized delegates bounded independent tasks; avoid concurrent edits to the same files.
- Communicate in the user's language, lead with the result, and keep progress/final
  reports concise. Report actual checks and remaining limitations, not assumed success.
- Before edits, inspect `git status --short`. Preserve unrelated changes and temporary
  artifacts. Do not commit, push, open a PR, or deploy unless requested.

These agreements apply the [official GPT-6 Astra prompting guidance](https://developers.openai.com/api/docs/guides/latest-model)
on autonomy, instruction conflicts, communication, delegation, and proportionate
verification. They are project choices, not a special model-required file format.
[Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
explains scope and overrides. Do not change the application's model defaults merely
because the coding agent uses Astra.

## Read the right source

- [README.md](README.md): setup, capabilities, configuration and operations.
- [docs/README.md](docs/README.md): current reference versus historical evidence.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): boundaries and code locations.
- [CONTEXT.md](CONTEXT.md) and [docs/adr/](docs/adr/): domain terms and accepted decisions.
- [CONTRIBUTING.md](CONTRIBUTING.md): reproducible commands and test isolation.
- Manifests, code, SQL and workflows establish what the checkout implements.
  Docs explain intent; a disagreement is a finding to reconcile, not permission to
  silently change behavior. Plans, changelog entries and `.jules/` journals are
  historical material, not a fresh task or proof of today's deployment.
- `GEMINI.md` points here. Do not maintain a second conflicting rulebook.
  Machine/plugin-installed skills are not repository implementation facts.

## Setup and verification

- Runtime: Python `>=3.14,<3.15`; pinned package manager: `uv==0.12.6`.
  `pyproject.toml` is the dependency manifest; `uv.lock` is the exact graph.
- Setup: `python -m pip install "uv==0.12.6"`, then `uv sync --locked`.
  Do not regenerate the lockfile or upgrade dependencies for unrelated work.
- Focused test: `uv run --locked pytest tests/test_<area>.py --override-ini="addopts=" --timeout=30`.
  Replace the path with an existing relevant test.
- Unit/E2E: `uv run --locked pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts=" --timeout=30`.
- Python gates: `uv run --locked ruff check .`,
  `uv run --locked ruff format --check .`, `uv run --locked mypy app bot.py`.
- Integration: `uv run --locked pytest tests/ -m integration -n 0 --override-ini="addopts=" --timeout=30`,
  only against an explicitly isolated `TEST_DATABASE_URL` (and test Redis as needed).
  Missing services/skipped tests are not passes. Tests may execute destructive DDL/DML.
- `pytest.ini` defaults to `-n auto --dist=loadgroup --basetemp=.pytest_tmp --timeout=30`.
  Overriding `addopts` clears those flags; keep a timeout explicitly when needed.
- Start with the smallest meaningful check. Broaden for cross-cutting behavior,
  schema, auth, provider or lifecycle changes. Prose-only changes need encoding,
  links, factual checks and `git diff --check`, not the entire runtime suite.
- Once required checks pass, repeat or expand only for new changes/failures/concerns.
  Never claim live Telegram/provider/VPS validation from local unit tests.
- Pre-commit and locked/CI tooling use Ruff 0.15.2. Install local hooks with
  `uv run --locked pre-commit install`; cloning alone does not install them.
  The documentation-encoding hook is read-only; Ruff hooks may apply fixes.
  Use locked lint/format check commands when a read-only check is intended.

## Implementation boundaries

- `bot.py` owns lifecycle/registration; `app/bot_commands.py` owns public menu/help
  identities, not handler execution. Keep RU/EN descriptions in `app/i18n.py`.
- Chat/research generation uses `app/providers/router.py` and typed requests/events.
  Preserve specialized integrations: explicit Crocodile Gemini selections use
  `google-genai` directly within the game boundary; image, embedding and Live/TTS
  paths have their own contracts. Do not force every SDK call through the chat router.
- Model role defaults and selector parsing live in `app/config.py`; explicit model
  catalog overrides live in `app/repos/models_repo.py`. Preserve env/default versus
  admin-override precedence and intentional `none` lists.
- `app/response_delivery/` owns streamed/completed Telegram response finalization.
  Do not reintroduce `app/streaming.py`, string/tuple terminal states, or handler
  edits that overwrite final publication/action rows. See ADR 0001.
- Reuse `app/utils/text_format.py`, `keyboards.py` and `messaging.py` where their
  contracts fit; domain-specific keyboards are valid. Runtime JSON compatibility
  helpers are in `app/utils/json_compat.py`; stdlib JSON in tools/tests is not
  categorically forbidden.
- Keep blocking I/O off async request paths. Use tracked background helpers in
  `app/utils/background_tasks.py` for detached work. Request-scoped tasks may be
  created directly when owned, cancelled and awaited on every exit path.
- `app/state.py` is process-local LRU state with PostgreSQL persistence and local
  locks, not a Redis-distributed UserState. Preserve lock ownership and persistence
  markers; Redis semaphores do not make arbitrary process state replica-safe.
- Memory lives in `app/repos/memory*.py`; `app/memory_manager.py` monitors process
  memory. Preserve durable consent epochs/leases, private-chat scope, deletion and
  live provenance. Never capture group messages into private LTM implicitly.
- Extraction/consolidation prepare external results before a write transaction.
  `memory_graph_writer.write_graph` uses the caller's connection, transaction,
  tenant context and source IDs; no hidden pool acquisition or provider calls.
  See ADR 0002.
- Numbered SQL is under `scripts/migrations/`; manifest validation is in
  `app/db/migration_manifest.py`. Add a unique valid migration name and update
  schema/RLS checks when appropriate. Preserve startup failure on migration,
  schema or RLS errors. Do not run migration commands as casual diagnostics:
  even `scripts/migrate.py --check` can create the tracking table.

## Secrets and privacy

Never print or commit `.env`, credentials, real chat contents, birth data, database
dumps or unredacted logs. Inspect example/config code instead. API keys use Fernet
derived from `ADMIN_SECRET` (`app/crypto.py`); changing the secret can make existing
keys unreadable. Telegram Mini App and web/admin guards are separate boundaries.
Preserve webhook token hashing, optional secret-header validation and deduplication.

Telegraph publishing is public and opt-in via `TELEGRAPH_PUBLICATION_ENABLED`
(default false), including Reader cold storage and natal mirrors. Keep that gate.
RLS is defense-in-depth with the current single migration/runtime DSN: privileged
roles can bypass it. Do not claim hard tenant isolation or universal erasure of
already published third-party copies.

## 🔴 UTF-8 integrity

All repository text files are UTF-8. In Python text-file I/O specify
`encoding="utf-8"`; in PowerShell use `Get-Content -Encoding UTF8`.
Binary I/O is not text encoding. Use UTF-8-safe patch tools for edits.

- Run `python scripts/check_encoding.py` before and after documentation edits.
  Without arguments it checks tracked and non-ignored new Markdown via Git:
  strict UTF-8, unexpected C0/C1 controls and known mojibake signatures.
  Explicit filenames check only those files (used by pre-commit). CI also runs it.
  It diagnoses without rewriting files; no heuristic detects every possible corruption.
- Never repair files solely because terminal emoji look garbled. Strict UTF-8
  decoding checks validity, not all forms of mojibake; inspect actual characters.
- Preserve raw emoji, Cyrillic and punctuation. Do not replace them with question
  marks or Unicode escapes, or run byte-level quote normalization.
- Windows default encoding depends on the interpreter/terminal. Set
  `$env:PYTHONUTF8 = "1"` before launching Python, or use `python -X utf8`.
  Changing `os.environ["PYTHONUTF8"]` inside a running interpreter does not
  reconfigure that interpreter. For display, `sys.stdout.reconfigure(encoding="utf-8")`
  affects stdout only.
