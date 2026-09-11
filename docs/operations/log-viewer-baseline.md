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

The user requested an immediate transition, so missing historical Portainer
measurements do not introduce a calendar soak gate. The deployment fails in the
same run if the new services do not reach the running state; operational capacity
is checked immediately with content-free `docker stats`, `docker system df`, and
the Grafana health/search checks from the runbook.

When recording a live baseline, retain only these aggregate fields outside the
VPS: timestamp, service memory/CPU, filesystem used/free, events/second, and
p50/p95/max encoded event bytes. Do not commit raw rows or screenshots containing
message content.
