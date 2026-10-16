# Voice admission and document outcomes — evidence appendix

This is dated local verification evidence. The [report](../../test-coverage-admission-document-2026-10-16.md)
explains behavior and limits; [evidence.json](evidence.json) carries exact commands,
phase outcomes, new case IDs, hashes, reviews and current gate outputs.

## Recorded run classification

Diagnostic passing assertions of bad behavior are not regression GREEN. Proper
RED cases assert desired behavior before the corresponding runtime edit.
Exploratory failures remain visible; only explicitly final passing cohorts
and the final full run support completion. Raw artifacts are retained locally
in the ignored workspace named by each JSON record.

| Run | Exit | Passing calls | Non-passing phases | Purpose |
| --- | ---: | ---: | ---: | --- |
| `audit-admission-diagnostic-01` | 0 | 3 | 0 | Three diagnostic probes: old voice overtaking and two passing controls for the existing network-stall policy. |
| `audit-admission-existing-01` | 0 | 83 | 0 | 83 existing admission, voice and processor cases before source changes, without coverage instrumentation. |
| `audit-admission-red-01` | 1 | 0 | 1 | Desired busy-rejection RED through real PTB dispatch: one intended failed call and two deselected probes, before the source edit. |
| `audit-documents-results` | 0 | 50 | 0 | 45 existing cases plus 5 diagnostic boundary scenarios. Error probes assert the old incorrect empty feedback; they are not regression GREEN. |
| `impl-documents-baseline` | 0 | 45 | 0 | 45 existing repository and destructive-callback cases before the source edit; selected branch baseline. |
| `impl-documents-final` | 0 | 58 | 0 | Initial document GREEN: 45 existing plus 13 new cases. The final covering cohort later adds the leaking predecessor and its correction. |
| `impl-documents-fix-final` | 0 | 59 | 0 | Exploratory 59-case GREEN with the defensive fixture workaround. That workaround was removed from the final design. |
| `impl-documents-fix-green` | 0 | 2 | 0 | Exploratory two-case GREEN with a defensive new-fixture rebind; superseded by restoration at the legacy mock's source. |
| `impl-documents-fix-legacy-final` | 0 | 59 | 0 | Final 59-case document cohort after scoped legacy mock restoration. The 13-case new module retains its reviewed bytes and assertions. |
| `impl-documents-fix-red` | 1 | 1 | 1 | Minimal order-dependent reproduction: the legacy callback predecessor passes, then the new real-menu assertion fails. |
| `impl-documents-red-callback-interface` | 1 | 0 | 1 | Permanent desired callback error-feedback RED after exhausted real-manager InterfaceError retries, before the source correction. |
| `impl-documents-red-callback-postgres` | 1 | 0 | 1 | Permanent desired callback error-feedback RED after exhausted real-manager PostgresError retries, before the source correction. |
| `impl-documents-red-facade-interface` | 1 | 0 | 1 | Permanent desired InterfaceError propagation RED at the real facade, before the source correction. |
| `impl-documents-red-facade-postgres` | 1 | 0 | 1 | Exploratory setup failure from vars() on slotted UserState. No desired assertion ran; this is not genuine RED. |
| `impl-documents-red-facade-postgres-2` | 1 | 0 | 1 | Permanent desired PostgresError propagation RED at the real facade, before the source correction. |
| `impl-voice-baseline-01` | 0 | 83 | 0 | 83 existing cases before the source edit; selected cb_voice branch baseline. |
| `impl-voice-final-01` | 0 | 112 | 0 | Final voice cohort: 83 existing plus 29 new cases, with selected branch coverage. |
| `impl-voice-green-01` | 1 | 12 | 2 | Exploratory attempt: 12 passing calls, one failed call and one teardown failure from a new transcript-only test-double signature. The double was corrected. |
| `impl-voice-green-02` | 0 | 29 | 0 | All 29 permanent voice cases pass after the source correction and test-double repair. |
| `impl-voice-red-01` | 1 | 0 | 1 | Permanent desired busy-rejection RED before the source correction: one failed call with passing setup and teardown. |
| `offline-final` | 1 | 1786 | 1 | First full attempt: 1786 passing calls and one failure from a leaked legacy menu mock, with seven deselected cases. This is not final GREEN. |
| `offline-final-02` | 0 | 4756 | 0 | Final frozen serial offline non-integration full suite after the legacy isolation correction, without coverage instrumentation. |

## Reproduction and boundaries

Use the locked commands from [CONTRIBUTING.md](../../../CONTRIBUTING.md). The
existing [offline recorder](../2026-10-09/offline_audit.py) owns synthetic
configuration, serial execution, socket guarding and retained results. Give
each invocation a fresh `GEMAIBOT_AUDIT_OUTPUT_DIR`.

The final full command runs without coverage instrumentation:

```powershell
uv run --locked python -X utf8 docs/test-coverage/2026-10-09/offline_audit.py focus tests/ --ignore=tests/integration -m 'not integration'
```

The JSON preserves its complete argument vector. Selected
baseline/final branch runs use `COVERAGE_CORE=ctrace` and unique coverage data;
cohort membership differs and is not whole-project coverage.

Real registered PTB dispatch uses synthetic bot/lifecycle/transport boundaries.
Real document call chains use a fake external driver and synthetic rows.
Cancellation-before-mutation and local resource release do not settle actual
database commit/rollback or remote Telegram acceptance. Distributed workers,
live services and integration targets were outside verification.

Review/documentation status: **approved; zero open findings; documentation checks passed**.
