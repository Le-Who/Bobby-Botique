# ADR 0004 — Keep host services and deployment recovery independent

**Status** Accepted

## The local Telegram Bot API has its own state

The cloud and local Telegram Bot API backends maintain independent webhook
state. Deploy runs the cloud release guard before it reuses or starts the local
server, then registers the bot on that local server. This prevents two backends
from delivering the same update. Reuse requires a readiness probe because a
running container can still be restarting. The application image probes over
`tg-net` because the third-party server image does not provide a guaranteed
HTTP probe command.

The server uses UID and GID 101 for its shared media. The application runs as a
dedicated user in group 101. Deploy grants group access to the server's media
directories so the bot can read files even when the server created a directory
with mode 0700.

Sources [deploy workflow](../../.github/workflows/deploy.yml),
[application image](../../Dockerfile) and
[cloud release guard](../../scripts/release_cloud_bot_api.py).

## Credential transfer and mounts

The workflow base64-encodes `ADMIN_SECRET` before passing it through the SSH
action and decodes it on the host. Encoding preserves shell-sensitive characters
through the action's variable forwarding. The optional Vertex ADC mount needs a
UID-readable host copy because a private host file is readable only to its
numeric owner inside the container.

Source [deploy workflow](../../.github/workflows/deploy.yml).

## Database and recovery gates

The candidate image runs the numbered migrations before a new bot container can
receive traffic. Its migration runner sees the same code and dependencies the
candidate will use. The workflow exits on migration failure and leaves any
recovery container from an earlier interruption for an operator to inspect.
Replacing it could discard the known recovery image.

Sources [deploy workflow](../../.github/workflows/deploy.yml) and
[migration runner](../../scripts/migrate.py).

## Redis has an independent lifecycle

A bot push stages the Redis configuration and leaves Redis recreation to an
operator. The deploy lock covers this project, including operator Redis
migration. Maintenance timers remain on the host across bot recreations. The bot
ACL permits application commands while reserving Redis reconfiguration and
database-wide deletion for the maintenance user.

The snapshot budget includes unrelated files in the private directory. Pruning
owns only regular files with names created by the snapshot command and preserves
the latest valid recovery point even if staging or validating its replacement
fails. Recovery checks label disposable containers because Docker may create one
even when its response is lost. Latency probes use the maximum PING round-trip
time reported by `redis-cli`, excluding Docker command startup.

Migration creates recovery files exclusively and compares live values and
expirations again before marking success. Operator commands emit bounded
diagnostics to the systemd journal because Docker diagnostics and exception
strings can carry configuration or data.

Sources [Redis operations](../../ops/redis/README.md),
[installation](../../ops/redis/install.py),
[maintenance](../../ops/redis/maintenance.py) and
[migration](../../ops/redis/migrate.py).

## Private observability

The host creates and retains the Grafana password independently of the staged
configuration. The parent directory stays root-only. The password file grants
group 0 read access because Grafana's pinned image runs as UID 472 and GID 0.
The advisory CI probe also reads its synthetic password as the runner.

Alloy filters Docker stdout by the opt-in label and the stable workload name.
Other local projects use the same opt-in label, so the name filter keeps their
events out of the bot's stream. The collector carries the application writer's
redacted events. Ingestion verification checks both components at that boundary.

Sources [private log search runbook](../../ops/observability/README.md),
[collector configuration](../../ops/observability/config.alloy) and
[advisory CI](../../.github/workflows/ci-advisory.yml).
