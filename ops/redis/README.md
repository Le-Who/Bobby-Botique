# VPS Redis operations

This stack belongs exclusively to GemAI Bot. The application uses Redis for
queues, quota admission, locks and caches; Redis is operational state, not a
disposable cache. PostgreSQL remains the durable user/memory database.

## Ownership and lifecycle

| Resource | Owner/name |
| --- | --- |
| Host configuration | `/opt/gemaibot-redis` (root-only) |
| Compose project | `gemaibot-redis` |
| Redis / status log containers | `gemaibot-redis` / `gemaibot-redis-monitor` |
| Internal Docker network | `gemaibot-redis` |
| Persistent external Docker volume | `gemaibot-redis-data` |
| Redis limits | 256 MiB data, noeviction; 768 MiB container, 0.5 CPU |
| Backup archive | `/opt/gemaibot-redis/backups`, root-only |

The network has no published Redis port. `tg-bot` joins both `tg-net` (Telegram
and outbound access) and the dedicated Redis network. No fixed subnet/IP or
other project's network is used. The status-log container has no network.
The bot and maintenance use different ACL identities; the bot cannot change
configuration, ACLs, replication or flush databases.

`install.py` refuses foreign ownership labels, partial credentials, missing
migrated volumes and mismatched migration identities. It never rotates existing
passwords and uses Compose `--no-recreate`. Bot deploys read the VPS-owned
`bot.env`; the old GitHub `REDIS_URL` secret is no longer forwarded. The deploy
lock is `/run/lock/gemaibot-deploy.lock`. Updating Redis itself requires a separate
reviewed operation; pushing bot code does not replace the running Redis image.

Redis, its volume and systemd timers survive bot recreation and VPS reboot.
Never use `compose down -v`, global Docker prune, or rename another project's
resources. The installer checks names/labels before making changes.

## Backups and monitoring

The user selected local VPS backups. There is no off-host copy or host-loss
guarantee. AOF with fsync every second protects normal process restarts; local
RDB snapshots provide point-in-time recovery from application/operator errors.

The hourly timer creates a BGSAVE snapshot, checks the copied RDB, and publishes
it atomically. Retention keeps up to 24 hourly buckets plus the newest snapshot
per UTC day for seven days, subject to a total 2 GiB archive budget. Overlapping
hourly/daily copies are stored once. The latest valid copy is always retained.
Capacity is reserved for the next copy; older snapshots may be pruned before
copying if the dataset has grown. Oversize snapshots, low disk, failed copying or
validation fail closed and preserve the latest valid copy. These are retention limits, not a
requirement to keep a month of data.

```sh
sudo systemctl status gemaibot-redis-backup.timer gemaibot-redis-monitor.timer
sudo python3 /opt/gemaibot-redis/maintenance.py backup
sudo python3 /opt/gemaibot-redis/maintenance.py restore-check
sudo python3 /opt/gemaibot-redis/maintenance.py monitor
```

`restore-check` starts a separately labelled UUID container with no networking or
published ports, loads the latest snapshot and checks Redis, then removes only
that temporary container. It never restores over production. A live recovery
requires stopping writers, restoring into a separate owned volume, verifying
contents/expirations, then switching the bot under the deploy lock.

The monitor timer runs every minute and records bounded metadata in `status/`.
It checks PING, maximum latency over a short redis-cli sample (excluding Docker
exec startup overhead), memory (80% threshold), AOF persistence, available disk
(1 GiB threshold), backup age (two hours), backup failures and archive size.
Failure, including a monitor status older than three minutes, is reflected in
the status stream and the private Grafana
**GemAI Redis and backups** dashboard (`/d/gemaibot-redis`). Alloy collects only
the exact monitor container; records contain no keys, values or credentials.
No new external notification channel is configured.

`/health` preserves the process/database liveness contract. `/ready` additionally
requires configured, reachable Redis; probes have a two-second Redis deadline.
The deploy checks `/ready` before accepting the candidate. Neither endpoint
proves Telegram/provider generation works.

## Migration and rollback

1. Check neighboring container IDs, active deploys and queue ownership. Acquire
   the project deploy lock. Stage configuration and run `install.py` without
   `--require-ready` to create the isolated empty destination.
2. Stop every writer to the source Redis gracefully. Keep the old Redis and a
   root-only source connection file for recovery; never print its contents.
3. Run `migrate.py` in the application image with protected `SOURCE_REDIS_URL` and
   `DESTINATION_REDIS_URL`. It refuses a nonempty destination, checks clock skew,
   writes an exclusive protected recovery snapshot, uses DUMP/RESTORE ABSTTL and
   verifies logical values/types and absolute expirations. Supported types are
   strings, hashes, sets, sorted sets and lists; other types require a verifier.
4. Give the four existing `croc:cache:*` hashes a non-renewing 30-day expiration,
   matching the bounded runtime cache policy. Set a random migration identity in
   `gemaibot:infrastructure:id` and the same value in `migration-complete` only
   after verification. Create/check a backup before accepting the destination.
5. Recreate only `tg-bot` with the VPS `bot.env` and dedicated network. Keep the
   old container stopped with restart disabled. Enable the timers using
   `install.py --require-ready`; check bot, queues, quotas and neighboring IDs.

Before any destination writes, a failed switch can return to the unchanged
source. After destination writes, rollback requires a verified reverse copy or
restoring the current local snapshot; merely restarting a container with the old
Redis URL would lose new state and split quota/queue ownership.

## Runtime safeguards

The Redis client does not replay uncertain commands automatically. Safe cache
operations and idempotent queue mutations own bounded retries explicitly.
Queue Lua uses cached scripts with NOSCRIPT recovery. Empty worker polling backs
off to five seconds while local admissions wake the relevant lane immediately;
remote/delayed work may wait up to five seconds plus Redis response time.

Game quota admission is atomic. A configured Redis outage cannot open a second
process-local budget. Game cache hashes keep their formats with bounded
capacity, incremental legacy pruning and non-renewing 30-day expiry. Reader
content and durable Telegraph mappings retain their existing contracts.
