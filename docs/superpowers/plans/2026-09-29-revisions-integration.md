# Revisions integration plan

**Goal:** Evaluate the two supplied revision bundles against checkout `25e54f8a`,
integrate useful working changes, and document decisions and remaining work.

**Architecture:** Keep the Python bot and existing delivery/observability owners.
Adapt isolated tools from the review consoles into offline repository checks.
A request-owned research budget spans model fallbacks; it does not determine
whether Telegram delivery succeeded.

**Execution:** Inline in the authorized working tree. No commits, deployment,
database operations, live providers or additional operator web service.
Use locked Python 3.14 tooling; preserve the supplied `revisions/` files.

- [x] Audit both consoles, Python patches and the reference checkout; record
  accepted/adapted/deferred/rejected decisions in a dated review.
- [x] Add regression tests for request-wide research time/page/token admission,
  missing usage, provider/tool cancellation and fallback. Implement a validated
  budget in `app/core/research_budget.py`, integrate `agentic.py` and
  `handlers/ai_search.py`, retain cancellation and response ownership.
- [x] Verify configuration findings against effective readers. Implement static
  registry/checks with negative fixtures; reconcile duplicated defaults without
  changing effective model choices or treating explicit overrides as drift.
- [x] Adapt the documentation link check with current/historical scope, heading
  anchors and negative fixtures. Add offline checks to CI after local validation.
- [x] Evaluate the synthetic quality cases as an offline manual evaluation kit;
  retain useful rubric/cases without claiming a measured provider baseline.
- [x] Update README, domain context, documentation index and roadmap with actual
  implementations, acceptance criteria and unresolved operational requirements.
- [x] Run focused tests, locked lint/format/mypy, appropriate offline suite,
  encoding and diff checks. Record real outcomes and limitations.

Each implementation step starts with a failing behavioral test where behavior
changes. Historical reports and UI seed values are inputs to review, not proof.

## Final scope adjustment

The user clarified that functional bot improvements must take precedence over
dashboards and research. Added full private memory reading and fixed explicit
document selection through the Q&A pipeline. Rewrote roadmap around actual
user flows. Evaluated the eval kit but did not introduce a standalone kit/console:
its rubric belongs with individual functional deliveries. The env snapshot is
explicitly scoped to literal app/bot readers and current Docker forwarding;
it does not pretend to generate a complete deployment or safe effective defaults.

Results and limitations: [dated review](../../revisions-review-2026-09-29.md).
