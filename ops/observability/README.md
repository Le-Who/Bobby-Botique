# Private log search

This stack replaces the Portainer **Logs** tab with bounded search in Grafana.
It does not replace Portainer container actions or shell access.

```text
tg-bot stdout (NDJSON)
  -> Docker local driver (20 MiB x 5)
  -> restricted Docker API proxy
  -> Grafana Alloy
  -> Loki (7-day retention)
  -> Grafana on VPS 127.0.0.1:3000
```

Only containers labeled `com.gemaibot.logs=true` are discovered. The deploy
workflow applies that label only to `tg-bot`; short-lived migration and release
containers are intentionally outside the first version.

## Immediate production transition

After CI succeeds on `vps_testai`, `.github/workflows/deploy.yml` packages this
directory, installs it at `/opt/gemaibot-observability`, creates a Grafana admin
password once, starts the four services, verifies that they are running, and then
replaces the bot container. There is no soak-period or calendar wait.

The generated password is never printed by CI. Retrieve it in a private SSH
session on the VPS:

```bash
cat /opt/gemaibot-observability/secrets/grafana_admin_password
```

Create the tunnel **from a terminal on your PC** and keep that terminal open.
The command intentionally shows no shell or success message while the tunnel is
active; `ExitOnForwardFailure` makes it stop immediately if local port 3000 cannot
be bound. Then open `http://127.0.0.1:3000`; the initial username is `admin`:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:3000:127.0.0.1:3000 VPS_USER@VPS_HOST
```

The tunnel is encrypted even though the browser URL is HTTP. Grafana is bound to
loopback and Loki, Alloy, and the Docker proxy publish no host ports.

## Search workflow

The provisioned **GemAI Bot Logs** dashboard opens the last 15 minutes, loads at
most 100 newest rows, and does not auto-refresh or live-tail. Use its environment,
level, event, release, and request-ID filters. Expand a row only when the full
protected JSON record is needed.

Useful Explore queries:

```logql
{service_name="gemaibotv2"} | json | level=~"(?i)error|critical"
{service_name="gemaibotv2"} | json | request_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
{service_name="gemaibotv2"} | json | actual_model="gemini-example"
{service_name="gemaibotv2"} | json | event=~"logging[.](loss_summary|sink_failed|format_failed)"
```

Loki indexes only the low-cardinality `service_name`, `environment`, `level`,
and `parse_status` labels. Request, trace, event, actor, release, instance, model,
and key identifiers remain JSON fields. Always keep a bounded time window when
filtering those fields.

## Storage and failure behavior

- Loki retains data for 168 hours. Compactor deletion is asynchronous and has a
  two-hour safety delay; 168 hours is not an exact physical-erasure timestamp.
- Docker continues to rotate its own local source window. If Loki or Alloy is
  unavailable longer than that window, old entries can be lost.
- Alloy persists source positions and uses its experimental bounded WAL. It can
  reduce gaps but does not provide exactly-once delivery; correlate duplicates
  by `event_id`.
- The Loki query API returns at most 500 entries and uses a 10-second timeout.
- Raw logs can contain bounded message text. Anyone with Grafana datasource
  access can read that protected stream; hiding a table field is not access
  control.

The starting resource ceilings are 1024 MiB for Loki, 512 MiB for Grafana,
256 MiB for Alloy, and 64 MiB for the Docker proxy. Check current consumption
without printing log contents:

```bash
docker stats --no-stream gemaibot-observability-loki-1 gemaibot-observability-grafana-1 gemaibot-observability-alloy-1 gemaibot-observability-docker-proxy-1
docker system df
```

## Synthetic checks and bounded export

Generate the standard load sizes without contacting application dependencies:

```bash
uv run --locked python scripts/smoke_log_viewer.py generate --count 100 --output smoke-100.ndjson
uv run --locked python scripts/smoke_log_viewer.py generate --count 1500 --output smoke-1500.ndjson
uv run --locked python scripts/smoke_log_viewer.py generate --count 10000 --output smoke-10000.ndjson
uv run --locked python scripts/smoke_log_viewer.py stats --input smoke-10000.ndjson
```

`stats` prints only counts, rates, and byte percentiles. The `query` command
rejects windows over seven days and limits over 500. `extract` converts a saved
Loki streams response to clean NDJSON and reports duplicates/missing expected
event IDs; its output can then be passed to `scripts/log_incident.py`.

## Manual validation and recovery

On a Docker host, set the secret-file path and validate before a manual start:

```bash
export GRAFANA_ADMIN_PASSWORD_FILE=/opt/gemaibot-observability/secrets/grafana_admin_password
chmod 0640 "$GRAFANA_ADMIN_PASSWORD_FILE"
export DOCKER_SOCKET_GID="$(stat -c '%g' /var/run/docker.sock)"
docker compose -f /opt/gemaibot-observability/compose.yml config --quiet
docker compose -f /opt/gemaibot-observability/compose.yml up -d --remove-orphans --force-recreate --wait --wait-timeout 90
docker compose -f /opt/gemaibot-observability/compose.yml ps
```

To return immediately to Docker/Portainer log viewing, stop only this independent
project. Keep the volumes so the collected history remains recoverable:

```bash
docker compose -f /opt/gemaibot-observability/compose.yml stop
```

Do not use `down -v` during recovery. The bot continues writing Docker logs when
the viewer is stopped.

## Pinned images

Checked against upstream registries on 2026-09-11:

- Grafana `13.2.1` — `sha256:f772d434e8fab0049deb2b1b30abd43342bcfca1537614aa8d36080232cf4283`
- Loki `3.7.4` — `sha256:87f0a067673756a3cede1bcbf0c74875f7df9b09fddb53e399d0c576f756cfcc`
- Alloy `v1.18.0` — `sha256:491b0578c04983fd54fe99b587b6fab4404dc46d0dc16677bd6b00cc1140b308`
- HAProxy `3.2.6-alpine` — `sha256:3a819392f5a6af2a9203d1c63ff0feba74f0c940623a558511be06d7d85e361c`

CI validates Compose plus the Alloy, Loki, and HAProxy configurations using these
same immutable image references before the deployment workflow can run.
