# Webhook admission evidence — 2026-10-10

This appendix supports the [dated implementation report](../../webhook-admission-ownership-2026-10-10.md).
It covers the owned webhook claim-to-queue operation, graceful lifecycle cleanup
and provider override failure contracts. It preserves all earlier audit snapshots.

[evidence.json](evidence.json) records 4,570 offline passes, 29 real Redis
contract passes, 118 browser cases, 54 exact new nodeids, measured offline
statement/branch coverage, current changed-file hashes and unchanged historical
artifact hashes. All selected setup/call/teardown phases pass; there are no skips
or collection errors. The source/config/Python-test snapshot contains 857 files
and is stable before/after execution.

The new cases are 11 ownership/cleanup units, 17 actual webhook/lifecycle routes,
6 real Redis contracts and 20 provider override failure contracts. These counts
come from the passing sets rather than summing overlapping focused runs.
The previous corpus has 163 cases unexecuted here: six collector E2E and 157
other integration cases, including PostgreSQL and legacy queue Lua tests.

The provider review approves its test contracts. The original admission review
records one Important exceptional-cleanup cancellation gap; a genuine RED,
the corrected 101-pass focused run and the separate ADDRESSED/APPROVE rereview
close that finding. These original and correction snapshots remain distinct.
Focused RED/GREEN, corrected mutation probes, scoped review packages, the full
857-file hash snapshot, command-result records and raw logs remain in the ignored
local workspace `.superpowers/sdd/2026-10-10-webhook-admission-ownership/`.

Offline and service results use Python 3.14.3, uv 0.12.6 and locked packages.
The exact pytest arguments, launch configuration, durations, versions and
source/evidence hashes are in the JSON. Repository-wide Ruff, format, mypy,
registry, encoding, link and diff gates are recorded separately from runtime
tests. The owned Redis process/executable identity was checked before stop;
later process and port checks confirmed termination. Its data was preserved.

The parent requested the mandatory-browser flag, but this dated runner clears
it before unit execution. The launch metadata explicitly distinguishes the
parent environment from the effective unset flag. All 118 browser cases actually
passed with zero skips; their measured passes establish this run's browser result.
This reporting correction was found during independent final evidence review.

These checks use synthetic credentials. Offline tests deny repository `.env`
access and Python network connections; Redis cases use an owned loopback service
and UUID-scoped keys in database 15. These are Python audit guards, not an OS
sandbox. Browser subprocesses use local fixtures. No PostgreSQL, collector E2E,
live Telegram/provider or deployment verification is claimed for this follow-up.

The guarantee covers cooperative caller cancellation and graceful drain. The
process-local queue remains vulnerable to process kill/crash and unknown Redis
acknowledgements; durable or exactly-once delivery is not established.
