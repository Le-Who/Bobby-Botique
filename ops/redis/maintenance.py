#!/usr/bin/env python3
"""Host-only, stdlib Redis backups and bounded health status (Python >=3.11).

Run as the owner of /opt/gemaibot-redis. Snapshots remain on this VPS: they do
not protect against host loss. Command output and Redis content are never logged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

CONTAINER = "gemaibot-redis"
OWNER_LABEL = "com.gemaibot.redis"
TEMP_LABEL = "com.gemaibot.redis.maintenance"
TEMP_PREFIX = "gemaibot-redis-maint-"
GIB = 1024**3
SNAPSHOT_NAME = re.compile(r"redis-(\d{8}T\d{12}Z)\.rdb\Z")
PINNED_IMAGE = re.compile(r"(?:docker\.io/library/)?redis:[A-Za-z0-9_.-]+@sha256:[0-9a-f]{64}\Z")
AUTH_COMMAND = 'REDISCLI_AUTH="$(cat /run/secrets/admin_password)" exec redis-cli --user ops --raw "$@"'


class MaintenanceError(Exception):
    """Only fixed, content-free error codes cross the operational boundary."""


def run_command(command, *, timeout=30, capture=True, file_limit=None):
    """Suppress stderr and never include subprocess output in exceptions."""
    limit = None
    if file_limit is not None:
        if os.name == "nt":
            raise MaintenanceError("unsupported_host")
        import resource

        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))

    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            preexec_fn=limit,
        )
    except (OSError, subprocess.SubprocessError):
        raise MaintenanceError("command_failed") from None
    if result.returncode:
        raise MaintenanceError("command_failed")
    data = result.stdout or b""
    if len(data) > 65536:
        raise MaintenanceError("response_too_large")
    return data.decode("utf-8", errors="replace").strip()


def utc_now():
    return datetime.now(UTC)


def timestamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_path(path):
    """Reject link traversal, including Windows junctions used in offline tests."""
    if ".." in path.parts:
        raise MaintenanceError("unsafe_path")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise MaintenanceError("unsafe_path")


def regular_file(path):
    safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise MaintenanceError("unsafe_path")
    return info


def number(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise MaintenanceError("invalid_response") from None
    if not 0 <= result <= 2**63 - 1:
        raise MaintenanceError("invalid_response")
    return result


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Maintenance:
    def __init__(self, root, *, runner=run_command, now=utc_now, disk_free=None):
        self.root = Path(root).absolute()
        self.runner = runner
        self.now = now
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.monotonic = time.monotonic
        self.sleep = time.sleep
        self.max_bytes = 2 * GIB
        self.backups = self.root / "backups"
        self.status = self.root / "status"
        for directory in (self.root, self.backups, self.status):
            safe_path(directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not directory.is_dir():
                raise MaintenanceError("unsafe_path")
            directory.chmod(0o700)

    @contextmanager
    def lock(self):
        path = self.root / ".maintenance.lock"
        safe_path(path)
        if path.exists():
            regular_file(path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise MaintenanceError("locked") from None
            else:
                import fcntl

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise MaintenanceError("locked") from None
            yield
        finally:
            os.close(descriptor)

    def image(self):
        path = self.root / "image.txt"
        if regular_file(path).st_size > 256:
            raise MaintenanceError("invalid_image")
        image = path.read_text(encoding="utf-8").strip()
        if not PINNED_IMAGE.fullmatch(image):
            raise MaintenanceError("invalid_image")
        return image

    def live_owned(self):
        labels = json.loads(self.runner(["docker", "inspect", "--format", "{{json .Config.Labels}}", CONTAINER]))
        if not isinstance(labels, dict) or labels.get(OWNER_LABEL) != "true":
            raise MaintenanceError("ownership_mismatch")

    def redis(self, *arguments):
        return self.runner(["docker", "exec", CONTAINER, "sh", "-c", AUTH_COMMAND, "sh", *arguments]).strip()

    def info(self, *sections):
        response = self.redis("INFO", *sections)
        return dict(line.split(":", 1) for line in response.splitlines() if ":" in line and not line.startswith("#"))

    def snapshots(self):
        result = []
        for path in self.backups.iterdir():
            match = SNAPSHOT_NAME.fullmatch(path.name)
            if not match or path.is_symlink():
                continue
            try:
                when = datetime.strptime(match[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
                info = regular_file(path)
            except (ValueError, MaintenanceError):
                continue
            result.append((when, path, info.st_size))
        return sorted(result, reverse=True)

    def stored_bytes(self):
        # Count unrelated regular files against the budget, but never remove them.
        return sum(path.stat().st_size for path in self.backups.iterdir() if not path.is_symlink() and path.is_file())

    def prune(self, *, reserve_bytes=0):
        snapshots = self.snapshots()
        if not snapshots:
            return
        retained = {snapshots[0][1]}
        hours = set()
        days = set()
        today = self.now().astimezone(UTC).date()
        for when, path, _ in snapshots:
            hour = when.replace(minute=0, second=0, microsecond=0)
            if hour not in hours and len(hours) < 24:
                retained.add(path)
                hours.add(hour)
            day = when.date()
            if 0 <= (today - day).days < 7 and day not in days:
                retained.add(path)
                days.add(day)
        total = self.stored_bytes()
        # Delete only exact snapshot names in this private directory. Never links.
        for _, path, size in reversed(snapshots):
            if path not in retained:
                regular_file(path)
                path.unlink()
                total -= size
        for _, path, size in reversed(snapshots):
            if total + reserve_bytes <= self.max_bytes:
                break
            if path in retained and path != snapshots[0][1]:
                regular_file(path)
                path.unlink()
                total -= size
        if total > self.max_bytes:
            raise MaintenanceError("quota_exceeded")

    def write_status(self, filename, result):
        path = self.status / filename
        safe_path(path)
        previous = None
        if path.exists():
            if regular_file(path).st_size > 65536:
                raise MaintenanceError("invalid_status")
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        descriptor, temporary = tempfile.mkstemp(prefix=".status-", dir=self.status)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(result, output, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            sync_directory(self.status)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        old = (previous.get("outcome"), previous.get("issues")) if isinstance(previous, dict) else None
        new = (result["outcome"], result.get("issues"))
        if old != new:
            # Host CLI events use the systemd journal; no app runtime is imported.
            event = {"event": "redis.maintenance.changed", "operation": filename.removesuffix(".json"), **result}
            sys.stderr.write(json.dumps(event, separators=(",", ":")) + "\n")

    def remove_owned(self, name, token):
        if not re.fullmatch(r"[0-9a-f]{32}", token) or name != TEMP_PREFIX + token:
            raise MaintenanceError("unsafe_resource")
        labels = json.loads(self.runner(["docker", "inspect", "--format", "{{json .Config.Labels}}", name]))
        if not isinstance(labels, dict) or labels.get(TEMP_LABEL) != token:
            raise MaintenanceError("ownership_mismatch")
        self.runner(["docker", "rm", "--force", "--volumes", name], capture=False)

    @contextmanager
    def isolated(self, snapshot, entrypoint, *arguments):
        regular_file(snapshot)
        token = uuid.uuid4().hex
        name = TEMP_PREFIX + token
        command = [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            f"{TEMP_LABEL}={token}",
            "--network",
            "none",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--user",
            "0:0",
            "--log-driver",
            "none",
            "--tmpfs",
            "/data:rw,noexec,nosuid,size=1m",
            "--mount",
            f"type=bind,source={snapshot},target=/restore/dump.rdb,readonly",
            "--entrypoint",
            entrypoint,
            self.image(),
            *arguments,
        ]
        created = False
        try:
            self.runner(command)
            created = True
            self.runner(["docker", "start", name], capture=False)
            yield name
        finally:
            # A lost create response may still leave a container. Its UUID label
            # proves ownership before removal; a foreign resource is never touched.
            try:
                self.remove_owned(name, token)
            except MaintenanceError:
                if created:
                    raise

    def validate(self, snapshot):
        with self.isolated(snapshot, "redis-check-rdb", "/restore/dump.rdb") as name:
            if self.runner(["docker", "wait", name], timeout=120).strip() != "0":
                raise MaintenanceError("invalid_snapshot")

    def save_snapshot(self):
        deadline = self.monotonic() + 120
        while True:
            before = self.info("persistence")
            if number(before.get("rdb_bgsave_in_progress")) == 0:
                break
            if self.monotonic() >= deadline:
                raise MaintenanceError("snapshot_timeout")
            self.sleep(0.5)
        saves = number(before.get("rdb_saves"))
        if self.redis("BGSAVE") != "Background saving started":
            raise MaintenanceError("snapshot_failed")
        while self.monotonic() < deadline:
            current = self.info("persistence")
            if number(current.get("rdb_bgsave_in_progress")) == 0:
                if current.get("rdb_last_bgsave_status") != "ok" or number(current.get("rdb_saves")) <= saves:
                    raise MaintenanceError("snapshot_failed")
                return
            self.sleep(0.5)
        raise MaintenanceError("snapshot_timeout")

    def backup(self):
        result = {"timestamp": timestamp(self.now()), "size_bytes": 0, "outcome": "ok"}
        temporary = None
        try:
            with self.lock():
                self.image()
                self.live_owned()
                self.save_snapshot()
                size = number(self.runner(["docker", "exec", CONTAINER, "stat", "-c", "%s", "/data/dump.rdb"]))
                if size == 0 or size > self.max_bytes:
                    raise MaintenanceError("quota_exceeded")
                if self.disk_free(self.backups) < GIB + size:
                    raise MaintenanceError("disk_low")
                stored = self.stored_bytes()
                if size + stored > self.max_bytes:
                    snapshots = self.snapshots()
                    unrelated = stored - sum(item[2] for item in snapshots)
                    newest = snapshots[0][2] if snapshots else 0
                    if size + newest + unrelated > self.max_bytes:
                        raise MaintenanceError("quota_exceeded")
                    # Growth can consume the space reserved by the previous run.
                    # Free older copies before staging, preserving the newest
                    # valid snapshot even if the forthcoming copy/check fails.
                    self.prune(reserve_bytes=size)
                    sync_directory(self.backups)
                    if size + self.stored_bytes() > self.max_bytes:
                        raise MaintenanceError("quota_exceeded")
                descriptor, filename = tempfile.mkstemp(prefix=".snapshot-", suffix=".rdb", dir=self.backups)
                os.close(descriptor)
                temporary = Path(filename)
                self.runner(
                    ["docker", "cp", f"{CONTAINER}:/data/dump.rdb", str(temporary)],
                    timeout=120,
                    capture=False,
                    file_limit=size,
                )
                temporary.chmod(0o600)
                actual = regular_file(temporary).st_size
                if actual != size or self.stored_bytes() > self.max_bytes:
                    raise MaintenanceError("snapshot_changed")
                self.validate(temporary)
                with temporary.open("r+b") as copied:
                    os.fsync(copied.fileno())
                target = self.backups / f"redis-{self.now().astimezone(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.rdb"
                if target.exists() or target.is_symlink():
                    raise MaintenanceError("snapshot_collision")
                os.replace(temporary, target)
                temporary = None
                sync_directory(self.backups)
                self.prune(reserve_bytes=actual)
                sync_directory(self.backups)
                result["size_bytes"] = actual
        except MaintenanceError as error:
            result["outcome"] = str(error)
        except (OSError, ValueError):
            result["outcome"] = "backup_failed"
        finally:
            if temporary is not None and temporary.exists():
                regular_file(temporary)
                temporary.unlink()
        self.write_status("backup.json", result)
        return result

    def restore_check(self):
        result = {"timestamp": timestamp(self.now()), "outcome": "ok", "dbsize": None}
        try:
            with self.lock():
                snapshots = self.snapshots()
                if not snapshots:
                    raise MaintenanceError("backup_missing")
                snapshot = snapshots[0][1]
                self.validate(snapshot)
                with self.isolated(
                    snapshot,
                    "redis-server",
                    "--bind",
                    "127.0.0.1",
                    "--protected-mode",
                    "yes",
                    "--dir",
                    "/restore",
                    "--dbfilename",
                    "dump.rdb",
                    "--appendonly",
                    "no",
                    "--save",
                    "",
                    "--logfile",
                    "/dev/null",
                ) as name:
                    deadline = self.monotonic() + 60
                    while self.monotonic() < deadline:
                        try:
                            pong = self.runner(["docker", "exec", name, "redis-cli", "--raw", "PING"])
                        except MaintenanceError:
                            pong = ""
                        if pong.strip() == "PONG":
                            result["dbsize"] = number(
                                self.runner(["docker", "exec", name, "redis-cli", "--raw", "DBSIZE"])
                            )
                            break
                        self.sleep(0.5)
                    else:
                        raise MaintenanceError("restore_timeout")
        except MaintenanceError as error:
            result["outcome"] = str(error)
        except (OSError, ValueError):
            result["outcome"] = "restore_failed"
        self.write_status("restore.json", result)
        return result

    def monitor(self):
        result = {
            "schema_version": 1,
            "timestamp": timestamp(self.now()),
            "level": "info",
            "service": CONTAINER,
            "environment": "production",
            "event": "redis.health",
            "outcome": "ok",
            "issues": [],
            "ping_ok": False,
            "ping_ms": None,
            "memory_ratio": None,
            "aof_write_ok": False,
            "backup_age_seconds": None,
            "backup_bytes": 0,
            "backup_count": 0,
            "backup_total_bytes": 0,
            "backup_disk_free_bytes": None,
            "volume_disk_free_bytes": None,
        }
        issues = result["issues"]
        try:
            self.live_owned()
            result["ping_ok"] = self.redis("PING") == "PONG"
            if not result["ping_ok"]:
                issues.append("ping_failed")
            else:
                # Non-interactive redis-cli exits after this sampling interval.
                # Its maximum PING RTT excludes Docker exec startup overhead.
                fields = self.redis("--latency", "-i", "0.1").split()
                if len(fields) != 4:
                    raise MaintenanceError("invalid_latency")
                minimum, maximum, average, samples = map(float, fields)
                if not all(math.isfinite(value) for value in (minimum, maximum, average, samples)) or not (
                    0 <= minimum <= average <= maximum and samples >= 1
                ):
                    raise MaintenanceError("invalid_latency")
                result["ping_ms"] = maximum
                if maximum > 250:
                    issues.append("ping_slow")
            info = self.info("memory", "persistence")
            used, maximum = number(info.get("used_memory")), number(info.get("maxmemory"))
            if maximum == 0:
                issues.append("memory_unbounded")
            else:
                result["memory_ratio"] = round(used / maximum, 4)
                if used / maximum >= 0.8:
                    issues.append("memory_high")
            result["aof_write_ok"] = info.get("aof_enabled") == "1" and info.get("aof_last_write_status") == "ok"
            if not result["aof_write_ok"]:
                issues.append("aof_write_failed")
            disk = self.runner(["docker", "exec", CONTAINER, "df", "-Pk", "/data"])
            result["volume_disk_free_bytes"] = number(disk.splitlines()[-1].split()[3]) * 1024
            if result["volume_disk_free_bytes"] < GIB:
                issues.append("volume_disk_low")
        except (MaintenanceError, OSError, ValueError, IndexError):
            issues.append("redis_unavailable")
        try:
            result["backup_disk_free_bytes"] = self.disk_free(self.backups)
            if result["backup_disk_free_bytes"] < GIB:
                issues.append("backup_disk_low")
            snapshots = self.snapshots()
            result["backup_count"] = len(snapshots)
            result["backup_total_bytes"] = self.stored_bytes()
            if result["backup_total_bytes"] > self.max_bytes:
                issues.append("backup_quota_exceeded")
            if not snapshots:
                issues.append("backup_missing")
            else:
                when, _, size = snapshots[0]
                age = int((self.now().astimezone(UTC) - when).total_seconds())
                result["backup_age_seconds"] = max(0, age)
                result["backup_bytes"] = size
                if age < -60:
                    issues.append("backup_clock_invalid")
                elif age > 7200:
                    issues.append("backup_stale")
            backup_status = self.status / "backup.json"
            if backup_status.exists():
                if regular_file(backup_status).st_size > 65536:
                    raise MaintenanceError("invalid_status")
                last = json.loads(backup_status.read_text(encoding="utf-8"))
                if not isinstance(last, dict) or last.get("outcome") != "ok":
                    issues.append("backup_failed")
        except (MaintenanceError, OSError, ValueError):
            issues.append("backup_status_unavailable")
        if issues:
            result["outcome"], result["level"] = "unhealthy", "error"
        self.write_status("health.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "monitor", "restore-check"))
    parser.add_argument("--root", type=Path, default=Path("/opt/gemaibot-redis"))
    arguments = parser.parse_args(argv)
    try:
        service = Maintenance(arguments.root)
        result = getattr(service, arguments.operation.replace("-", "_"))()
    except Exception:
        # Never print exception strings: command/library failures can contain data.
        result = {"timestamp": timestamp(utc_now()), "outcome": "maintenance_failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0 if result["outcome"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
