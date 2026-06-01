# Maintainer Queue

This repository keeps active maintenance work visible through GitHub pull requests. The queue is intentionally labeled so reviewers can separate production-targeted work from generated, experimental, or staging-branch candidates.

Snapshot date: 2026-06-01.

## Current Open Pull Request Snapshot

| Group | Count |
| --- | ---: |
| Total open PRs | 127 |
| Targeting `vps_testai` | 11 |
| Targeting `TEST_gemaibotv2` | 116 |
| Security-related titles | 29 |
| Performance-related titles | 46 |
| Accessibility-related titles | 44 |
| Test-related titles | 9 |

## Queue Labels

- `needs-triage`: maintainer review required before merge or close.
- `codex-review`: useful candidate for Codex-assisted review.
- `production-target`: targets the maintained `vps_testai` branch.
- `test-branch`: targets `TEST_gemaibotv2` and may be experimental or generated work.
- `security`: security-sensitive title or branch.
- `performance`: allocation, query, latency, or throughput work.
- `accessibility`: dashboard, tab, WAI-ARIA, or screen-reader work.
- `tests`: test coverage or test infrastructure.
- `provider-routing`: AI provider, model, key, quota, fallback, or routing work.
- `telegram-mini-app`: Telegram Mini App, dashboard, WebSocket, or UI-surface work.
- `deployment`: Docker, VPS, runtime config, or operator workflow work.

## Triage Policy

1. Review `production-target` PRs before `test-branch` PRs.
2. Review `security` PRs before general performance or accessibility work.
3. Close a PR only after checking its diff against the current `vps_testai` branch.
4. Close duplicate generated PRs when a newer PR covers the same files and behavior.
5. Keep useful but non-urgent candidates labeled instead of merging them opportunistically.
6. Record validation commands in the PR before merge.

No PR should be closed solely because it is old or generated. Closure should be based on duplicate status, obsolete code paths, failed validation, or supersession by a newer merged change.
