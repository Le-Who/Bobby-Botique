# ADR 0005 — Separate dependency scheduling, upgrade risk and toolchains

**Status** Accepted

## Separate exact scheduling and upgrade risk

Dependabot's cron parser uses the Fugit weekday modulo extension for a
fourteen-day cadence across month boundaries. The independent frontier audit
uses a weekly UTC wakeup and checks eligibility against a fixed epoch. Manual
dispatch bypasses the date gate. The schedules serve different jobs. Dependabot
proposes dependency updates, and the frontier audit measures what the manifest
and resolver would allow before changing the production dependency graph.

The grouped Python updates are limited to minor and patch releases. Current
pre-1.0 dependencies are excluded from the group because a minor update can
break callers before the dependency's stable release. Evaluate those updates
individually.

Use canonical package names in `pyproject.toml`. Dependabot's uv updater has
failed to rewrite a manifest requirement when its case differs from the
canonical name in `uv.lock`. The repository validates the spelling in
both direct dependency scopes.

Sources [Dependabot configuration](../../.github/dependabot.yml),
[frontier workflow](../../.github/workflows/dependency-frontier.yml) and
[Dependabot's fortnightly scheduling discussion](https://github.com/dependabot/dependabot-core/issues/12246).
See also [the uv updater's name-matching issue](https://github.com/dependabot/dependabot-core/issues/14119).

## Toolchain boundaries

The bot image installs the exact production graph from `uv.lock`. Redis
maintenance runs on the Ubuntu host, so its Ruff target is Python 3.12 while
the bot targets Python 3.14. Ruff formatting is scoped to code. External review
bundles and Markdown keep their own review and encoding checks.

The Google Gen AI SDK's lower bound is 2.25.0 because current TTS requests
send the Interactions API's speech metadata. Earlier tested serialization lost
the speech annotation. Keep both the lower bound and the actual-SDK transport
regressions when changing that dependency. [TTS runtime controls](../runtime-controls.md)

Ruff leaves guarded imports in place. Some configuration failures occur before
logging is configured, so the startup error path uses standard output.
Interactive diagnostics and benchmarks also need standard output. The project
allows explicit `try/except` cleanup and places files that need a longer-lived
handle under their owning code's lifetime.

Sources [project configuration](../../pyproject.toml),
[production image](../../Dockerfile) and [verification guide](../../CONTRIBUTING.md).
