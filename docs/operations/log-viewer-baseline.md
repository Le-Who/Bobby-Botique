# Log viewer capacity baseline

This file records the safe evidence boundary for the initial log-viewer rollout.
No production log body, environment, credential, DSN, or unrestricted container
inspection is stored in the repository.

## Pre-deployment evidence

- Application event ceiling: 32768 UTF-8 bytes by default.
- Docker source rotation: 20 MiB × 5 files for `tg-bot`.
- Browser default: 100 rows from the last 15 minutes, auto-refresh off.
- Loki server result ceiling: 500 rows; query timeout: 10 seconds.
- Service memory ceilings: Loki 1024 MiB, Grafana 512 MiB, Alloy 256 MiB,
  Docker proxy 64 MiB.
- Production VPS memory, filesystem headroom, event rate, and Portainer browser
  heap were not available in the repository and are not claimed as measured.

The checked-in deployment has no calendar soak gate. It fails if the services do
not pass startup/health, internal readiness or Grafana authentication checks.
These gates are not capacity measurements or proof of end-to-end event ingestion.
Operators can collect capacity evidence with content-free `docker stats`,
`docker system df` and bounded synthetic searches from the runbook; this audit
did not run those commands against a live host.

When recording a live baseline, retain only these aggregate fields outside the
VPS: timestamp, service memory/CPU, filesystem used/free, events/second, and
p50/p95/max encoded event bytes. Do not commit raw rows or screenshots containing
message content.
