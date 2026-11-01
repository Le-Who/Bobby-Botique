"""Idempotent host installer. Never replaces live Redis or another project's resources."""

# ruff: noqa: T201 -- bounded operator CLI output, never command diagnostics.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

IMAGE = "redis:8.8.4-alpine@sha256:ab669f1420eaa05360a417b24f3bd210c9272e2c79a4bb35d3e50b4bfd50dbee"
LABEL = "com.gemaibot.redis=true"
NETWORK = "gemaibot-redis"
VOLUME = "gemaibot-redis-data"


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if result.returncode:
        # Docker diagnostics may include environment values. Never echo stderr.
        raise RuntimeError(f"Command failed: {args[0]} {args[1]} (exit {result.returncode})")
    return result.stdout.strip()


def inspect(kind: str, name: str) -> dict | None:
    result = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True, timeout=15)
    if result.returncode:
        # Missing and inaccessible are distinguished before any mutation.
        run("docker", "info", "--format", "{{.ServerVersion}}")
        return None
    return json.loads(result.stdout)[0]


def validate_resource(resource: dict, kind: str) -> None:
    labels = resource.get("Config", {}).get("Labels", {}) if kind == "container" else resource.get("Labels", {})
    if not labels or labels.get("com.gemaibot.redis") != "true":
        raise RuntimeError(f"Redis {kind} ownership conflict; no resources changed")
    if kind == "network" and resource.get("Internal") is False:
        raise RuntimeError("Redis network must be internal")


def prepare_credentials(root: Path, *, existing_data: bool) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / "secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    required = [directory / name for name in ("app_password", "admin_password", "users.acl")]
    required.append(root / "bot.env")
    present = [p.exists() and not p.is_symlink() for p in required]
    if all(present):
        return
    if existing_data or any(present):
        raise RuntimeError("Redis credentials incomplete; refusing credential rotation")
    app_password, admin_password = secrets.token_hex(32), secrets.token_hex(32)
    for name, value in [("app_password", app_password), ("admin_password", admin_password)]:
        path = directory / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o444 if name == "admin_password" else 0o600)
    # Only the host maintenance user may reconfigure Redis or remove whole databases.
    app_hash = hashlib.sha256(app_password.encode()).hexdigest()
    admin_hash = hashlib.sha256(admin_password.encode()).hexdigest()
    acl = directory / "users.acl"
    acl.write_text(
        "user default off\n"
        f"user ops on #{admin_hash} ~* &* +@all\n"
        f"user bot on #{app_hash} ~* &* +@all -config -acl -flushall -flushdb "
        "-shutdown -module -debug -replicaof -slaveof -save -bgsave -bgrewriteaof\n",
        encoding="utf-8",
    )
    acl.chmod(0o444)
    path = root / "bot.env"
    path.write_text(
        f"REDIS_URL=redis://bot:{app_password}@gemaibot-redis:6379/0\nREDIS_MAX_CONNECTIONS=64\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def require_migration(root: Path) -> None:
    if not (root / "migration-complete").is_file():
        raise RuntimeError("Redis migration not completed; refusing deployment")


def validate_ready_volume(existing: dict | None) -> None:
    if existing is None:
        raise RuntimeError("Migrated Redis volume missing; restore required before deployment")


def install(root: Path, *, require_ready: bool) -> None:
    if require_ready:
        require_migration(root)
    existing = {}
    for kind, name in [
        ("network", NETWORK),
        ("volume", VOLUME),
        ("container", NETWORK),
        ("container", "gemaibot-redis-monitor"),
    ]:
        current = inspect(kind, name)
        if current:
            validate_resource(current, kind)
        existing[(kind, name)] = current
    if require_ready:
        validate_ready_volume(existing[("volume", VOLUME)])
    prepare_credentials(root, existing_data=bool(existing[("volume", VOLUME)]))
    (root / "status").mkdir(mode=0o700, exist_ok=True)
    (root / "backups").mkdir(mode=0o700, exist_ok=True)
    (root / "image.txt").write_text(IMAGE + "\n", encoding="utf-8")
    if not existing[("network", NETWORK)]:
        run("docker", "network", "create", "--internal", "--label", LABEL, NETWORK)
    if not existing[("volume", VOLUME)]:
        run("docker", "volume", "create", "--label", LABEL, VOLUME)
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            "--user",
            "0",
            "-v",
            f"{VOLUME}:/data",
            IMAGE,
            "-c",
            "chown 999:999 /data",
        )
    run("docker", "compose", "-f", str(root / "compose.yml"), "config", "--quiet")
    run(
        "docker",
        "compose",
        "-f",
        str(root / "compose.yml"),
        "up",
        "-d",
        "--no-recreate",
        "--wait",
        "--wait-timeout",
        "60",
        "redis",
    )
    # The stateless log sidecar may pick up monitoring changes independently.
    run("docker", "compose", "-f", str(root / "compose.yml"), "up", "-d", "--wait", "monitor-log")
    if require_ready:
        expected = (root / "migration-complete").read_text(encoding="utf-8").strip()
        actual = run(
            "docker",
            "exec",
            NETWORK,
            "sh",
            "-c",
            'REDISCLI_AUTH="$(cat /run/secrets/admin_password)" exec redis-cli --user ops --raw GET gemaibot:infrastructure:id',
        )
        if len(expected) != 32 or actual != expected:
            raise RuntimeError("Redis migration identity missing; refusing empty or wrong database")
    # Timers are owned independently of bot container recreation.
    for name in ("backup", "monitor"):
        for extension in ("service", "timer"):
            filename = f"gemaibot-redis-{name}.{extension}"
            destination = Path("/etc/systemd/system") / filename
            destination.write_text((root / filename).read_text(encoding="utf-8"), encoding="utf-8")
    run("systemctl", "daemon-reload")
    if require_ready:
        run("systemctl", "enable", "--now", "gemaibot-redis-backup.timer", "gemaibot-redis-monitor.timer")
    print(json.dumps({"redis_infrastructure": "ready", "migration_complete": require_ready}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/gemaibot-redis"))
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        install(args.root.resolve(), require_ready=args.require_ready)
    except Exception as error:
        print(json.dumps({"redis_install": "failed", "error_type": type(error).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
