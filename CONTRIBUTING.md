# Contributing

Thanks for considering a contribution to GemAI Bot v2. This repository is maintained as a production-oriented Telegram AI assistant framework, so changes should preserve runtime safety, private-user data boundaries, and deployment repeatability.

## Before You Start

1. Check existing pull requests and issues for related work.
2. Keep changes focused: one behavior, bug fix, or docs update per pull request.
3. Never commit secrets, `.env` files, Telegram tokens, API keys, service-account JSON, database dumps, private chat logs, or real user exports.
4. Use fake adapters, fixtures, or redacted samples for tests.

## Local Setup

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

For full integration coverage, configure a test PostgreSQL database through `TEST_DATABASE_URL`. Unit tests should not require live Telegram, Gemini, OpenAI, Pollinations, Tavily, Jina, WeatherAPI, or other external provider calls.

## Validation

Run the narrowest test that proves your change first, then run broader checks before requesting review:

```bash
python -m ruff check .
python -m pytest tests/ --ignore=tests/integration -m "not integration" --override-ini="addopts="
```

For database, WebSocket, provider-routing, or migration changes, also run the relevant integration tests with `TEST_DATABASE_URL` configured:

```bash
python -m pytest tests/ --override-ini="addopts="
```

## Pull Request Checklist

- Describe the user-visible or maintainer-visible change.
- List the exact validation commands you ran and their results.
- Add or update tests when behavior changes.
- Update `README.md`, `CHANGELOG.md`, or `docs/` when public behavior, deployment, or operator workflow changes.
- Keep security-sensitive findings out of public PR text unless they are already responsibly disclosed.

## Code Style

- Prefer explicit async boundaries and deterministic background-task ownership.
- Keep provider/network calls behind adapters that can be mocked.
- Preserve Telegram UX recovery paths instead of surfacing raw provider exceptions.
- Use existing repositories, config helpers, i18n helpers, and test fixtures before adding new abstractions.
- Avoid broad refactors in bug-fix PRs.
