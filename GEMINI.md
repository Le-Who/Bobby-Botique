# Gemini coding-agent entry point

Read [AGENTS.md](AGENTS.md) for repository working agreements before editing.
It is the shared instruction source for coding agents, including Gemini and Codex.

For project context, use [README.md](README.md), the
[documentation index](docs/README.md), [architecture](docs/ARCHITECTURE.md),
[domain vocabulary](CONTEXT.md), and [contributor guide](CONTRIBUTING.md).

The former 2026-06-25 module map and duplicated invariants were replaced on
2026-09-08 because they disagreed with the runtime and current ownership boundaries.
This file does not configure the bot's Gemini models; those are defined in
`app/config.py` and explicit runtime model-catalog overrides.
