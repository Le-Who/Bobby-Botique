# Security Policy

## Supported Branch

Security maintenance targets `vps_testai`, the production branch configured in the checked-in workflows. Include your affected commit when reporting; you do not need to upgrade a live deployment before submitting a report. The current hosted default branch and private-reporting availability must be checked on GitHub, not inferred from this file.

## Reporting A Vulnerability

Do not publish exploit details, secrets, tokens, API keys, Telegram init data, database dumps, or private chat data in a public issue.

Preferred reporting path:

1. Open a private GitHub Security Advisory for this repository.
2. Include the affected commit or release, reproduction steps, expected impact, and any relevant logs with secrets redacted.
3. If private advisories are not available to you, open a minimal public issue that says you have a security report and avoid sensitive details until a private contact path is established.

## Implementation Boundaries (reviewed 2026-09-08)

- API keys are encrypted using Fernet derived from `ADMIN_SECRET`; preserve that
  secret when moving encrypted data. It is not an AES-GCM implementation.
- Private-memory consent, provenance and account deletion are application
  controls, not a blanket compliance or database-isolation guarantee. The current
  single migration/runtime DSN may use a role that bypasses RLS; see
  [database boundaries](docs/ARCHITECTURE.md#database-lifecycle-and-privacy-limits).
- Telegraph publication is public and disabled by default, including Reader
  mirrors. Local erasure cannot retract previously exported or third-party copies.
- Integration tests and migration tools can mutate schema/data. Use disposable
  targets as described in [CONTRIBUTING.md](CONTRIBUTING.md).

## High-Risk Areas

Please prioritize responsible disclosure for issues involving:

- Telegram `initData` validation and Mini App authentication;
- webhook routing and request signature handling;
- WebSocket authorization for Crocodile and Live Audio flows;
- provider API key storage, rotation, and logging;
- prompt/template injection that can leak private memory or secrets;
- long-term memory, pgvector, and group-chat privacy boundaries;
- file/document ingestion and media processing;
- Docker/VPS deployment configuration.

## Maintainer Response

The maintainer will triage security reports by reproducibility and impact. Critical auth, secret-handling, or remote-execution findings take priority over general hardening requests. Accepted fixes should include a regression test when practical and should not disclose exploit details in commit messages.
