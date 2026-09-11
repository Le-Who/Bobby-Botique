# Documentation index

Reviewed 2026-09-08 against checkout `8fc19516`. Read current references first;
historical plans, journals and test/deploy reports are not current instructions.

## Current references

| Document | Responsibility |
| --- | --- |
| [Repository README](../README.md) | Capabilities, setup, configuration and operations |
| [AGENTS.md](../AGENTS.md) | Shared repository working agreements for coding agents |
| [GEMINI.md](../GEMINI.md) | Entry-point pointer, not a second rulebook |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Locked tooling, checks, test safety and hook installation |
| [CONTEXT.md](../CONTEXT.md) | Domain vocabulary: delivery, state, consent, provenance and model roles |
| [Architecture](ARCHITECTURE.md) | Source-backed ownership boundaries and rationale |
| [Security policy](../SECURITY.md) | Reporting route and implementation limits |
| [Roadmap](../ROADMAP.md) | Future directions, explicitly not implemented behavior |
| [Pollinations / Crocodile](pollinations-daily-croc.md) | Authentication, shared text-model selection, image overrides |
| [Natal dependency decision](natal-chart-dependency-decision.md) | PyEphem/local math, city data, scope and dependency rationale |
| [Natal readiness](natal-chart-product-readiness.md) | Implemented checks and rollout checklist; old results explicitly historical |
| [Audit report](documentation-audit-2026-09-08.md) | Evidence, corrections, rationale and remaining limits |
| [Logging runbook](logging.md) | Runtime configuration, privacy policy and incident investigation for operators/agents |
| [Private log search](../ops/observability/README.md) | Grafana/Loki/Alloy deployment, access, search limits and recovery |
| [Log viewer capacity baseline](operations/log-viewer-baseline.md) | Safe capacity facts and live-measurement boundary |
| [Log event catalog](log-events.md) | Stable schema-v1 event ownership, required fields and terminal semantics |
| [Logging audit](logging-audit-2026-09-10.md) | Dated source audit, findings and target design rationale |

## Accepted architecture decisions

- [ADR 0001](adr/0001-single-owner-ai-response-delivery.md): single-owner AI delivery;
  clarified that all Telegraph publication is opt-in.
- [ADR 0002](adr/0002-provenance-safe-memory-graph-writes.md): caller-owned atomic
  memory graph/provenance writes. The decision remains applicable.

ADRs preserve the problem and rejected alternatives. A design proposal does not
silently override an accepted decision or describe deployed behavior.

## Historical material and where to verify it now

All older files in `superpowers/specs/` and `superpowers/plans/` retain their original
bodies with an archival notice. Approval labels and unchecked steps are historical;
neither authorizes execution now nor proves a feature is missing.

| Historical topic | Current evidence boundary |
| --- | --- |
| 2026-05-06 draw pre-canvas | `app/handlers/cmd_image.py`, `app/handlers/cb_image.py` |
| 2026-05-24 horoscope intent | `app/natal/intent.py`, `app/astro.py`, horoscope handlers; not the original external API proposal |
| 2026-06-07 natal implementation | `app/natal/`, dependency decision and readiness above; not the planned Swiss library stack |
| 2026-06-25 daily admin/broadcast | `app/web.py`, `app/templates/admin_daily.html`, scheduled/daily handlers |
| 2026-07-16 inline tarot routing | `app/handlers/inline.py`, `tests/test_tarot_inline_retry.py` |
| 2026-08-14 keyboard preservation / AI delivery | `app/response_delivery/`, ADR 0001; old streaming helpers are removed |
| 2026-08-14 model catalog | `app/config.py`, `app/repos/models_repo.py`, `app/handlers/cmd_models.py` |
| 2026-08-17 trivia model routing | `app/games/daily_ai.py`, `app/games/daily_trivia_authoring.py` |
| 2026-08-25 LTM safety | `app/repos/memory_consent.py`, memory workflows, migrations 067–069 |
| 2026-08-27 hardening / LTM writer | ADR 0002, `app/repos/memory_graph_writer.py`, command catalog and CI |
| 2026-08-29 migration invariants | `app/db/migration_manifest.py`, `app/db/schema.py`, CI migration gates |
| 2026-08-29 trivia / admin observability | Daily trivia authoring, horoscope handlers, dashboard and focused tests |
| 2026-08-29 dependency frontier | `pyproject.toml`, `uv.lock`, dependency scripts and workflows |

[CHANGELOG.md](../CHANGELOG.md), [maintainer queue](MAINTAINER_QUEUE.md) and
`.jules/` journals are historical evidence. Old performance figures and broad
optimization advice need remeasurement/review before reuse. Do not rewrite old
release entries merely to make their versions match today.

## Non-Markdown evidence

`natal-reference-fixture.example.json` is an unverified template;
`natal-reference-fixture.moira-jpl.json` is the committed external reference fixture.
`natal-city-overrides.example.json` describes optional city overrides. Keep data
fixtures intact during prose audits; they are consumed by code/deployment checks.

## Maintenance rule

Change each fact at its owner: dependencies in manifest/lock, behavior in code,
commands in the public catalog, schema in migrations, deployment in workflows.
Update the corresponding current guide and record dated evidence where needed.
Do not maintain undated file counts, provider prices/quotas or test-pass totals as
project-state authority.
