# Priority coverage evidence — 2026-10-15

This appendix supports the [dated report](../../test-coverage-priority-2026-10-15.md).

[evidence.json](evidence.json) derives 28 exact new passing nodeids from pytest
phase reports: eight per-source graph attribute cases, six per-user memory task
cancellation cases, eight placeholder-admission cases and six Long Read visibility
cases. It includes focused baselines, genuine defect REDs, passing neighbors,
independent 28-case verification, source hashes and current measured coverage.

The final whole offline confirmation passed **4,612 cases**, including **118
browser-marked cases**, with no failure or skip. Seven integration-marked cases
were deselected; the integration directory was excluded. The instrumented
coverage run is retained separately: **4,611 passed and one existing CI discovery
test timed out** while launching its second subprocess collection under a shared
25-second deadline. The standalone retry and final whole-suite confirmation
passed with the selector and time limits unchanged. No failed run is relabeled
as a full passing coverage run.

Measured Python statement coverage is 31,762 / 44,647 = 71.1403%; branch coverage
is 7,885 / 13,078 = 60.2921%; the combined coverage.py measure is 68.6825%.
These metrics belong to the explicitly recorded instrumented run. Both whole
runs used a stable 756-file selected source/test/config and historical-evidence
snapshot, verified before and after execution. Older coverage reports use
different snapshots and are not a same-snapshot percentage baseline.

All subagents used `gpt-6.1-sol`, with effort selected by task complexity. The
fresh read-only reviewer approved the runtime/test changes with no findings and
independently passed all 28 new cases. Locked Ruff/format, Mypy and configuration
registry checks passed. Final UTF-8/link/diff checks are recorded in the report.

Raw synthetic recorder logs, exact phase reports, RED/GREEN evidence, coverage
data and full hash snapshots remain in the ignored local directory
`.superpowers/sdd/2026-10-15-priority-coverage/`. The public JSON retains hashes and
bounded metadata; it does not publish unrestricted logs or live data.

Offline runs use synthetic settings, deny repository `.env` access and record
zero blocked Python socket attempts. Browser/Node subprocesses are not an OS
network sandbox. PostgreSQL/Redis/collector integration and live external-service
behavior were not executed. Graph tests verify the actual Python-to-SQL payload;
real SQL trigger deletion/recomposition needs disposable PostgreSQL evidence.
