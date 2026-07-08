# Security Policy

## Supported Branch

Security fixes target the public default branch, `vps_testai`. If you run a fork or a pinned deployment, upgrade to the latest default-branch commit before reporting a vulnerability unless the issue is still reproducible there.

## Reporting A Vulnerability

Do not publish exploit details, secrets, tokens, API keys, Telegram init data, database dumps, or private chat data in a public issue.

Preferred reporting path:

1. Open a private GitHub Security Advisory for this repository.
2. Include the affected commit or release, reproduction steps, expected impact, and any relevant logs with secrets redacted.
3. If private advisories are not available to you, open a minimal public issue that says you have a security report and avoid sensitive details until a private contact path is established.

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
