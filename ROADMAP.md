# Maintainer Roadmap

This roadmap records public maintainer priorities for GemAI Bot v2. It is not a release promise; it exists to make the maintenance burden and intended OSS direction visible.

## Near-Term Maintenance

- Triage the open pull-request queue and close stale or superseded maintenance branches.
- Keep `README.md`, `CHANGELOG.md`, and deployment notes aligned with the current `vps_testai` runtime.
- Add release labels and issue labels for provider routing, Telegram Mini App, Crocodile, Live Audio, memory, security, and deployment work.
- Publish an anonymized usage-metrics section after exporting aggregate deployment counts safely.

## Reliability And Testing

- Expand regression coverage around provider failover, partial streaming failures, webhook recovery, and WebSocket reconnect behavior.
- Keep fast unit tests independent from live Telegram or LLM providers.
- Harden integration tests so database-dependent failures are isolated from unit validation.
- Add repeatable smoke scripts for deployed VPS health checks.

## Security

- Review Telegram `initData` validation, WebSocket auth, webhook handling, and admin-only command paths.
- Add focused tests for provider key redaction and secret-safe logging.
- Document production deployment hardening for Docker, reverse proxy, Redis, PostgreSQL, and service-account files.
- Use private GitHub Security Advisories for sensitive reports.

## Provider And Model Portability

- Add an OpenAI provider path behind the existing provider-routing boundary.
- Add evals that compare provider responses without leaking private user data.
- Keep provider-specific model lists configurable through environment or admin settings.
- Document fallback behavior and quota boundaries per provider.

## Documentation

- Split the large README into smaller topic docs while keeping the top-level overview readable.
- Add diagrams for request flow, streaming recovery, memory retrieval, and Mini App authentication.
- Document common operator tasks: deployment, key rotation, metrics review, daily Crocodile operations, and live-audio diagnostics.
