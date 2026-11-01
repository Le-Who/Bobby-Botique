"""Offline behavior checks for the host Redis maintenance CLI."""

import ast
import importlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

NOW = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)
IMAGE = "redis:8.8.4-alpine@sha256:" + "a" * 64
GIB = 1024**3


@pytest.fixture
def maintenance():
    source = Path(__file__).parents[1] / "ops" / "redis" / "maintenance.py"
    assert source.exists(), "Redis backup and monitoring CLI is not implemented"
    return importlib.import_module("ops.redis.maintenance")


class Docker:
    """Docker boundary fake: no network, host commands, or credential reads."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.snapshot = b"REDIS0012synthetic-snapshot"
        self.save_failed = False
        self.check_failed = False
        self.restore_failed = False
        self.saves = 2
        self.used_memory = 20
        self.maxmemory = 100
        self.aof_status = "ok"
        self.owned = True
        self.removed = []
        self.latency = "0 1 0.27 11"

    def __call__(self, command, *, timeout=30, capture=True, file_limit=None):
        self.calls.append(command)
        assert command[0] == "docker"
        verb = command[1]
        if verb == "inspect":
            name = command[-1]
            if name == "gemaibot-redis":
                return json.dumps({"com.gemaibot.redis": "true" if self.owned else "false"})
            if name not in self.containers:
                raise RuntimeError("missing synthetic container")
            return json.dumps({"com.gemaibot.redis.maintenance": self.containers[name]["token"]})
        if verb == "exec":
            if command[3:6] == ["stat", "-c", "%s"]:
                return str(len(self.snapshot))
            if command[3:5] == ["df", "-Pk"]:
                return (
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/fake 9000000 1 8000000 1% /data\n"
                )
            if command[2] != "gemaibot-redis":
                if self.restore_failed:
                    return "LOADING Redis is loading the dataset in memory"
                return "PONG" if command[-1] == "PING" else "7"
            operation = command[7]
            assert command[3:5] == ["sh", "-c"]
            assert (
                command[5] == 'REDISCLI_AUTH="$(cat /run/secrets/admin_password)" exec redis-cli --user ops --raw "$@"'
            )
            if operation == "BGSAVE":
                self.saves += 1
                return "Background saving started"
            if operation == "PING":
                return "PONG"
            if operation == "--latency":
                return self.latency
            assert operation == "INFO"
            return (
                f"rdb_bgsave_in_progress:0\nrdb_saves:{self.saves}\n"
                f"rdb_last_bgsave_status:{'err' if self.save_failed else 'ok'}\n"
                f"used_memory:{self.used_memory}\nmaxmemory:{self.maxmemory}\n"
                f"aof_enabled:1\naof_last_write_status:{self.aof_status}\n"
            )
        if verb == "cp":
            assert command[2] == "gemaibot-redis:/data/dump.rdb"
            assert file_limit == len(self.snapshot)
            Path(command[3]).write_bytes(self.snapshot)
            return ""
        if verb == "create":
            name = command[command.index("--name") + 1]
            token = command[command.index("--label") + 1].split("=", 1)[1]
            assert command[command.index("--network") + 1] == "none"
            assert command[command.index("--memory") + 1] == "512m"
            assert command[command.index("--memory-swap") + 1] == "512m"
            assert command[command.index("--log-driver") + 1] == "none"
            assert "--read-only" in command
            assert not {"-p", "--publish", "-P", "--publish-all"}.intersection(command)
            assert IMAGE in command
            self.containers[name] = {"token": token, "checker": "redis-check-rdb" in command}
            return "b" * 64
        if verb == "start":
            return "b" * 64
        if verb == "wait":
            return "1" if self.check_failed else "0"
        if verb == "rm":
            name = command[-1]
            assert name in self.containers
            self.removed.append(name)
            del self.containers[name]
            return name
        raise AssertionError(f"Unexpected Docker verb: {verb}")


@pytest.fixture
def setup(maintenance, tmp_path):
    root = tmp_path / "redis"
    root.mkdir()
    (root / "image.txt").write_text(IMAGE + "\n", encoding="utf-8")
    docker = Docker()
    service = maintenance.Maintenance(root, runner=docker, now=lambda: NOW, disk_free=lambda _: 10 * GIB)
    return service, docker, root


def snapshot(root, when, size=20):
    directory = root / "backups"
    directory.mkdir(exist_ok=True)
    path = directory / f"redis-{when.strftime('%Y%m%dT%H%M%S%fZ')}.rdb"
    path.write_bytes(b"x" * size)
    return path


@pytest.mark.parametrize("latency,slow", [("0 1 0.27 11", False), ("0 300 27.27 11", True)])
def test_monitor_measures_redis_latency_without_docker_startup(setup, latency, slow):
    service, docker, root = setup
    snapshot(root, NOW)
    docker.latency = latency
    ticks = iter([0, 0.8])
    service.monotonic = lambda: next(ticks)
    result = service.monitor()
    assert result["ping_ms"] == float(latency.split()[1])
    assert ("ping_slow" in result["issues"]) is slow


@pytest.mark.parametrize("latency", ["", "ERR denied", "0 nan 0 10", "0 1 0 0"])
def test_monitor_rejects_invalid_latency_sample(setup, latency):
    service, docker, root = setup
    snapshot(root, NOW)
    docker.latency = latency
    assert "redis_unavailable" in service.monitor()["issues"]


def test_backup_publishes_only_validated_snapshot_and_private_status(setup):
    service, docker, root = setup
    result = service.backup()
    assert result == {"timestamp": "2026-11-01T12:30:00Z", "size_bytes": 27, "outcome": "ok"}
    paths = list((root / "backups").glob("*.rdb"))
    assert len(paths) == 1
    assert paths[0].read_bytes() == docker.snapshot
    assert json.loads((root / "status" / "backup.json").read_text(encoding="utf-8")) == result
    assert docker.containers == {}
    if os.name != "nt":
        assert paths[0].stat().st_mode & 0o777 == 0o600
        assert (root / "status").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("failure,expected", [("save_failed", "snapshot_failed"), ("check_failed", "invalid_snapshot")])
def test_failed_snapshot_preserves_previous_backup(setup, failure, expected):
    service, docker, root = setup
    previous = snapshot(root, NOW - timedelta(hours=1))
    setattr(docker, failure, True)
    result = service.backup()
    assert result["outcome"] == expected
    assert previous.read_bytes() == b"x" * 20
    assert list((root / "backups").iterdir()) == [previous]
    assert docker.containers == {}


def test_low_disk_and_quota_refuse_copy_without_deleting_previous(setup):
    service, docker, root = setup
    previous = snapshot(root, NOW - timedelta(hours=1))
    service.disk_free = lambda _: GIB
    assert service.backup()["outcome"] == "disk_low"
    service.disk_free = lambda _: 10 * GIB
    service.max_bytes = 30
    assert service.backup()["outcome"] == "quota_exceeded"
    assert previous.exists()
    assert not any(command[1] == "cp" for command in docker.calls)


def test_retention_selects_hourly_and_daily_snapshots_and_reserves_next_copy(setup):
    service, _, root = setup
    paths = [snapshot(root, NOW - timedelta(hours=hour)) for hour in range(24)]
    daily = [snapshot(root, NOW - timedelta(days=day)) for day in range(1, 8)]
    duplicate = snapshot(root, NOW - timedelta(minutes=5))
    unrelated = root / "backups" / "operator-copy.rdb"
    unrelated.write_bytes(b"keep")
    service.prune(reserve_bytes=20)
    assert all(path.exists() for path in paths)
    # Yesterday's newest hourly snapshot also satisfies its daily bucket.
    assert not daily[0].exists()
    assert all(path.exists() for path in daily[1:6])
    assert not daily[6].exists()
    assert not duplicate.exists()
    assert unrelated.read_bytes() == b"keep"
    service.max_bytes = 64
    service.prune(reserve_bytes=20)
    assert paths[0].exists()
    assert sum(path.stat().st_size for path in (root / "backups").iterdir()) <= 44


def test_restore_check_loads_isolated_snapshot_and_cleans_owned_container(setup):
    service, docker, root = setup
    latest = snapshot(root, NOW)
    result = service.restore_check()
    assert result["outcome"] == "ok"
    assert result["dbsize"] == 7
    assert latest.exists()
    assert len(docker.removed) == 2
    assert docker.containers == {}
    assert all(name.startswith("gemaibot-redis-maint-") for name in docker.removed)


def test_restore_timeout_still_cleans_up_only_owned_resources(setup):
    service, docker, root = setup
    snapshot(root, NOW)
    docker.restore_failed = True
    tick = iter(range(1000))
    service.monotonic = lambda: next(tick)
    service.sleep = lambda _: None
    assert service.restore_check()["outcome"] == "restore_timeout"
    assert docker.containers == {}


def test_cleanup_rejects_namespace_and_label_mismatch(setup):
    service, docker, _ = setup
    with pytest.raises(Exception, match="unsafe_resource"):
        service.remove_owned("tg-bot", "a" * 32)
    name = "gemaibot-redis-maint-" + "a" * 32
    docker.containers[name] = {"token": "b" * 32}
    with pytest.raises(Exception, match="ownership_mismatch"):
        service.remove_owned(name, "a" * 32)
    assert name in docker.containers
    assert docker.removed == []


@pytest.mark.parametrize("age,issue", [(None, "backup_missing"), (timedelta(hours=3), "backup_stale")])
def test_monitor_marks_missing_or_stale_backup_unhealthy(setup, age, issue):
    service, _, root = setup
    if age is not None:
        snapshot(root, NOW - age)
    result = service.monitor()
    assert result["outcome"] == "unhealthy"
    assert issue in result["issues"]
    assert result["schema_version"] == 1
    assert result["service"] == "gemaibot-redis"
    assert result["event"] == "redis.health"
    assert result["level"] == "error"
    assert json.loads((root / "status" / "health.json").read_text(encoding="utf-8")) == result


def test_monitor_reports_only_bounded_health_fields(setup):
    service, docker, root = setup
    snapshot(root, NOW)
    docker.used_memory = 85
    docker.aof_status = "err"
    result = service.monitor()
    assert result["memory_ratio"] == 0.85
    assert result["issues"] == ["memory_high", "aof_write_failed"]
    assert result["ping_ok"] is True
    assert "password" not in json.dumps(result)
    assert not any(command[1] in ("create", "cp", "rm") for command in docker.calls)


def test_refuses_unowned_live_container_before_snapshot(setup):
    service, docker, _ = setup
    docker.owned = False
    assert service.backup()["outcome"] == "ownership_mismatch"
    assert not any(command[1] == "exec" for command in docker.calls)


def test_rejects_symlink_backup_directory_and_preserves_outside_data(maintenance, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.rdb"
    keep.write_bytes(b"keep")
    root = tmp_path / "redis"
    root.mkdir()
    try:
        (root / "backups").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer mode or administrator rights")
    with pytest.raises(Exception, match="unsafe_path"):
        maintenance.Maintenance(root)
    assert keep.read_bytes() == b"keep"


def test_lock_rejects_overlapping_backup_and_restore(setup):
    service, _, root = setup
    snapshot(root, NOW)
    with service.lock():
        assert service.backup()["outcome"] == "locked"
        assert service.restore_check()["outcome"] == "locked"


def test_interrupted_create_removes_matching_owned_container(setup, maintenance):
    service, docker, root = setup
    snapshot(root, NOW)

    def interrupted(command, **kwargs):
        result = docker(command, **kwargs)
        if command[1] == "create":
            raise maintenance.MaintenanceError("command_failed")
        return result

    service.runner = interrupted
    assert service.restore_check()["outcome"] == "command_failed"
    assert docker.containers == {}


def test_failed_copy_preserves_all_previous_snapshots(setup, maintenance):
    service, docker, root = setup
    previous = [snapshot(root, NOW - timedelta(hours=hour)) for hour in (1, 2)]

    def interrupted(command, **kwargs):
        if command[1] == "cp":
            Path(command[3]).write_bytes(b"partial")
            raise maintenance.MaintenanceError("command_failed")
        return docker(command, **kwargs)

    service.runner = interrupted
    assert service.backup()["outcome"] == "command_failed"
    assert set((root / "backups").iterdir()) == set(previous)


def test_rejects_unpinned_image_before_docker_operations(setup):
    service, docker, root = setup
    (root / "image.txt").write_text("redis:latest\n", encoding="utf-8")
    assert service.backup()["outcome"] == "invalid_image"
    assert docker.calls == []


def test_subprocess_failure_does_not_disclose_command_output(maintenance, capsys):
    with pytest.raises(maintenance.MaintenanceError, match="^command_failed$"):
        maintenance.run_command([sys.executable, "-c", "import sys; print('synthetic-private-payload'); sys.exit(1)"])
    assert "synthetic-private-payload" not in str(capsys.readouterr())


@pytest.mark.skipif(os.name == "nt", reason="Linux file-size limit applies on the VPS")
def test_subprocess_file_size_limit_bounds_partial_copy(maintenance, tmp_path):
    target = tmp_path / "partial.rdb"
    with pytest.raises(maintenance.MaintenanceError, match="^command_failed$"):
        maintenance.run_command(
            [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'x'*4096)", str(target)],
            file_limit=512,
        )
    assert target.stat().st_size <= 512


def test_cli_returns_failure_for_unhealthy_result_without_extra_output(setup, maintenance, monkeypatch, capsys):
    service, _, _ = setup
    monkeypatch.setattr(maintenance, "Maintenance", lambda _: service)
    assert maintenance.main(["monitor"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["issues"] == ["backup_missing"]


def test_monitor_healthy_success_and_no_repeated_state_change_log(setup, capsys):
    service, _, root = setup
    snapshot(root, NOW)
    assert service.monitor()["outcome"] == "ok"
    assert capsys.readouterr().err
    assert service.monitor()["outcome"] == "ok"
    assert capsys.readouterr().err == ""


def test_monitor_reports_backup_count_and_total_quota_usage(setup):
    service, _, root = setup
    snapshot(root, NOW, size=20)
    snapshot(root, NOW - timedelta(hours=1), size=30)
    service.max_bytes = 40
    result = service.monitor()
    assert result["backup_count"] == 2
    assert result["backup_total_bytes"] == 50
    assert result["issues"] == ["backup_quota_exceeded"]


def test_host_cli_parses_on_vps_python_312():
    source = Path(__file__).parents[1] / "ops" / "redis" / "maintenance.py"
    ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 12))


def test_growing_snapshot_prunes_older_copy_to_avoid_permanent_quota_refusal(setup):
    service, docker, root = setup
    newest = snapshot(root, NOW - timedelta(hours=1), size=50)
    older = snapshot(root, NOW - timedelta(hours=2), size=30)
    service.max_bytes = 100
    assert service.backup()["outcome"] == "ok"
    assert not older.exists()
    assert list((root / "backups").glob("*.rdb"))
    assert sum(path.stat().st_size for path in (root / "backups").iterdir()) <= 100
    assert any(path.read_bytes() == docker.snapshot for path in (root / "backups").iterdir())
    # Post-success retention may replace the former newest to reserve next time.
    assert not newest.exists() or newest.read_bytes() == b"x" * 50


def test_copy_failure_after_quota_pruning_preserves_newest_valid_snapshot(setup, maintenance):
    service, docker, root = setup
    newest = snapshot(root, NOW - timedelta(hours=1), size=50)
    older = snapshot(root, NOW - timedelta(hours=2), size=30)
    service.max_bytes = 100

    def interrupted(command, **kwargs):
        if command[1] == "cp":
            assert not older.exists()
            Path(command[3]).write_bytes(b"partial")
            raise maintenance.MaintenanceError("command_failed")
        return docker(command, **kwargs)

    service.runner = interrupted
    assert service.backup()["outcome"] == "command_failed"
    assert list((root / "backups").iterdir()) == [newest]
    assert newest.read_bytes() == b"x" * 50
