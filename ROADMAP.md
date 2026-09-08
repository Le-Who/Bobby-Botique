# Maintainer Roadmap

Reviewed 2026-09-08. This is a list of possible follow-up work, not an implementation
specification, release promise or authorization to make external changes.
Current behavior is described in [README.md](README.md) and
[architecture](docs/ARCHITECTURE.md).

## Maintenance gaps

- Keep pre-commit and locked/CI Ruff synchronized during future tool upgrades.
  The 2026-09-08 follow-up aligned the pin and added Markdown encoding checks to
  pre-commit and CI; expand detection only for demonstrated corruption cases.
- Consider a maintained `.env.example` (currently absent), synchronized with
  effective `load_settings()` defaults; distinguish deployment/runtime overrides.
- Triage the actual GitHub queue after fetching fresh data. The June snapshot in
  [MAINTAINER_QUEUE.md](docs/MAINTAINER_QUEUE.md) is historical.

## Reliability and operations

Provider recovery, typed response delivery, consent/provenance hardening,
migration validation, container smoke and dependency gates already exist.
Follow-up work should target demonstrated gaps rather than reimplementing them:

- Validate deployed provider/media/Live paths in a controlled operational window;
  automate repeatable checks where credentials and cleanup can be isolated.
- Revisit separate migrator/runtime database roles if hard database-enforced
  tenant isolation is required.
- Capture reproducible load/latency evidence before making performance promises.
- Recheck natal mobile/desktop flows and interpretation quality for each rollout.

## Optional product directions

- A dedicated direct OpenAI provider and redacted cross-provider evaluations.
- Multi-model debate/synthesis, if a product scope is approved.
- Explicitly consented shared group memory; this is not the existing private LTM
  behavior and would need its own authorization, retention and tenant model.

## Documentation

Keep current guides short and linked to source. Preserve ADR rationale and historical
plans/results with status labels. Publish deployment/adoption counts only with a
collection date, anonymization and permission; do not infer them from repository size.
