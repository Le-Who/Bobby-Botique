"""Filesystem and monitoring adapters keep runtime work bounded and accurate."""

import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app import cache, database
from app.utils import tg_file


async def test_local_telegram_file_io_runs_outside_event_loop(monkeypatch, tmp_path):
    local = tmp_path / "voice.ogg"
    local.write_bytes(b"synthetic audio")
    loop_thread = threading.get_ident()
    io_threads = []

    class LocalPath:
        def is_file(self):
            io_threads.append(threading.get_ident())
            assert io_threads[-1] != loop_thread, "local file metadata must not block the event loop"
            return local.is_file()

        def stat(self):
            return local.stat()

        def read_bytes(self):
            return local.read_bytes()

        def open(self, *args, **kwargs):
            return local.open(*args, **kwargs)

    monkeypatch.setattr(tg_file, "Path", lambda path: LocalPath())
    download = AsyncMock()
    result = await tg_file.get_file_bytes(
        SimpleNamespace(local_mode=True),
        SimpleNamespace(file_path="/var/lib/telegram-bot-api/voice.ogg", download_as_bytearray=download),
    )
    assert result == b"synthetic audio"
    assert io_threads and all(thread != loop_thread for thread in io_threads)
    download.assert_not_awaited()


async def test_local_file_growth_cannot_bypass_download_limit(monkeypatch, tmp_path):
    local = tmp_path / "grown.ogg"
    local.write_bytes(b"x" * 11)
    monkeypatch.setattr(tg_file, "MAX_TELEGRAM_FILE_BYTES", 10)

    class GrowingFile:
        def is_file(self):
            return True

        def stat(self):
            return SimpleNamespace(st_size=5)

        def read_bytes(self):
            return local.read_bytes()

        def open(self, *args, **kwargs):
            return local.open(*args, **kwargs)

    monkeypatch.setattr(tg_file, "Path", lambda path: GrowingFile())
    with pytest.raises(ValueError, match="too large"):
        await tg_file.get_file_bytes(
            SimpleNamespace(local_mode=True),
            SimpleNamespace(file_path="/var/lib/telegram-bot-api/grown.ogg"),
        )


async def test_database_monitor_reports_real_pool_pressure(monkeypatch, caplog):
    manager = database.db_manager
    pool = SimpleNamespace(
        _closed=False,
        get_min_size=lambda: 2,
        get_max_size=lambda: 10,
        get_size=lambda: 10,
        get_idle_size=lambda: 1,
    )
    monkeypatch.setattr(manager, "pool", pool)

    async def stop_after_sample(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(database.asyncio, "sleep", stop_after_sample)
    with caplog.at_level(logging.WARNING):
        await manager.monitor_connection_pool()
    assert "Database pool high utilization: 90.0%" in caplog.text


async def test_redis_stats_count_selected_database(monkeypatch):
    redis = SimpleNamespace(
        info=AsyncMock(return_value={"db0": {"keys": 99}, "db15": {"keys": 7}}),
        connection_pool=SimpleNamespace(connection_kwargs={"db": 15}),
    )
    monkeypatch.setattr(cache, "redis_client", redis)
    assert (await cache.get_cache_stats())["total_keys"] == 7


async def test_redis_stats_error_does_not_return_backend_credentials(monkeypatch):
    private_detail = "redis://fake-user:fake-password@private.example:6379"
    redis = SimpleNamespace(info=AsyncMock(side_effect=RedisConnectionError(private_detail)))
    monkeypatch.setattr(cache, "redis_client", redis)
    monkeypatch.setattr(
        cache, "_redis_operation_with_retry", AsyncMock(side_effect=cache.RedisConnectionError(private_detail))
    )
    result = await cache.get_cache_stats()
    assert result == {"error": "Redis connection issue"}
    assert "fake-password" not in str(result)
