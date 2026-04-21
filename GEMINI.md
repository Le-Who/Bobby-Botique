# gemaibotv2 — Gemini CLI Context

## Project
Python Telegram bot with Gemini AI backend. Stack: Python 3.12+, aiogram 3.x,
PostgreSQL (asyncpg + pgvector), Redis, Quart, Vertex AI, various LLM providers.

## Critical: UTF-8 Encoding (NEVER VIOLATE)

All files are UTF-8. **This has caused production incidents.**

```python
# ALWAYS
content = open("README.md", encoding="utf-8").read()
open("README.md", "w", encoding="utf-8").write(content)
```

- Windows terminal output showing garbled emoji is a DISPLAY bug, NOT file corruption.
- Never "convert" or "fix" encoding unless explicitly asked by the user.
- Never replace emoji with `?` or `\uXXXX` escape sequences.
- Pre-commit hook (`scripts/check_encoding.py`) will block commits with mojibake.

## Code Rules

- All async — use `asyncio`, `await`, `async with` throughout.
- Tests: `pytest` with `-n auto` for parallelism; markers `unit`, `integration`.
- Lint: `ruff check .` must be clean before any commit.
- Type checks: `python -m mypy app/ --ignore-missing-imports`.

## Project Commands

```bash
# Run tests (parallel)
pytest tests/ -n auto -q

# Lint
ruff check .

# Type check
python -m mypy app/ --ignore-missing-imports

# Check encoding
python scripts/check_encoding.py
```

## Key Modules

- `bot.py` — Dispatcher entrypoint, handler registration
- `app/handlers/` — Telegram update handlers (messages, callbacks, inline, commands)
- `app/providers/` — LLM provider abstraction (Gemini, Opencode, OpenRouter)
- `app/games/` — Crocodile game logic (word_bank.py, judge.py)
- `app/state.py` — UserState, Redis-backed distributed state
- `app/repos/` — Database repositories (asyncpg)
- `tests/` — 1800+ tests, mostly unit with some integration (`-m integration`)
