from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.memory_manager import (
    AutoCleanupResource,
    DocumentCache,
    MemoryManager,
    MemoryThreshold,
    add_memory_cleanup_callback,
    cleanup_memory,
    get_memory_stats,
    remove_memory_cleanup_callback,
    shutdown_memory_manager,
)


@pytest.fixture
def mock_psutil():
    with patch("app.memory_manager.psutil") as mock:
        process = MagicMock()
        process.memory_info.return_value.rss = 100 * 1024 * 1024
        process.memory_info.return_value.vms = 200 * 1024 * 1024
        process.memory_percent.return_value = 50.0
        process.cpu_percent.return_value = 10.0
        mock.Process.return_value = process

        virtual_memory = MagicMock()
        virtual_memory.available = 500 * 1024 * 1024
        virtual_memory.total = 1000 * 1024 * 1024
        mock.virtual_memory.return_value = virtual_memory
        yield mock


class TestMemoryManager:
    @pytest.fixture(autouse=True)
    async def setup_teardown(self):
        self.manager = MemoryManager()
        with patch("app.memory_manager.memory_manager", self.manager):
            yield
        await self.manager.stop()

    def test_initialization(self):
        assert self.manager.thresholds.warning_percent == 70.0
        assert self.manager.thresholds.critical_percent == 85.0
        assert self.manager.thresholds.cleanup_percent == 60.0
        assert self.manager._running is True
        assert self.manager._monitoring_task is not None

    @pytest.mark.asyncio
    async def test_custom_thresholds(self):
        thresholds = MemoryThreshold(warning_percent=80.0, critical_percent=90.0, cleanup_percent=50.0)
        manager = MemoryManager(thresholds)
        assert manager.thresholds.warning_percent == 80.0
        assert manager.thresholds.critical_percent == 90.0
        assert manager.thresholds.cleanup_percent == 50.0
        await manager.stop()

    def test_cleanup_callbacks(self):
        callback = MagicMock()
        self.manager.add_cleanup_callback(callback)
        assert callback in self.manager._cleanup_callbacks

        self.manager.remove_cleanup_callback(callback)
        assert callback not in self.manager._cleanup_callbacks

    def test_get_memory_stats(self, mock_psutil):
        stats = self.manager.get_memory_stats()
        assert stats["current_usage_mb"] == 100.0
        assert stats["current_usage_percent"] == 50.0
        assert stats["available_mb"] == 500.0
        assert stats["total_mb"] == 1000.0
        assert stats["cpu_percent"] == 10.0
        assert "history_size" in stats
        assert "last_cleanup" in stats
        assert "thresholds" in stats

    @pytest.mark.asyncio
    async def test_check_memory_usage_low(self, mock_psutil):
        mock_psutil.Process.return_value.memory_percent.return_value = 30.0
        self.manager.thresholds.cleanup_percent = 40.0

        self.manager._preventive_cleanup = AsyncMock()
        self.manager._suggested_cleanup = AsyncMock()
        self.manager._emergency_cleanup = AsyncMock()

        await self.manager._check_memory_usage()

        self.manager._last_cleanup = 0
        await self.manager._check_memory_usage()

        self.manager._preventive_cleanup.assert_called()
        self.manager._suggested_cleanup.assert_not_called()
        self.manager._emergency_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_memory_usage_warning(self, mock_psutil):
        mock_psutil.Process.return_value.memory_percent.return_value = 75.0
        self.manager.thresholds.warning_percent = 70.0

        self.manager._preventive_cleanup = AsyncMock()
        self.manager._suggested_cleanup = AsyncMock()
        self.manager._emergency_cleanup = AsyncMock()

        await self.manager._check_memory_usage()

        self.manager._preventive_cleanup.assert_not_called()
        self.manager._suggested_cleanup.assert_called()
        self.manager._emergency_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_memory_usage_critical(self, mock_psutil):
        mock_psutil.Process.return_value.memory_percent.return_value = 90.0
        self.manager.thresholds.critical_percent = 85.0

        self.manager._preventive_cleanup = AsyncMock()
        self.manager._suggested_cleanup = AsyncMock()
        self.manager._emergency_cleanup = AsyncMock()

        await self.manager._check_memory_usage()

        self.manager._preventive_cleanup.assert_not_called()
        self.manager._suggested_cleanup.assert_not_called()
        self.manager._emergency_cleanup.assert_called()

    @pytest.mark.asyncio
    async def test_callback_execution(self):
        async_callback = AsyncMock()
        self.manager.add_cleanup_callback(async_callback)

        await self.manager._suggested_cleanup()

        async_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_auto_cleanup_resource(self):
        class TestResource(AutoCleanupResource):
            def __init__(self):
                super().__init__("TestResource")
                self.cleaned = False

            def _perform_cleanup(self):
                self.cleaned = True

        resource = TestResource()

        assert len(self.manager._cleanup_callbacks) > 0

        await self.manager._suggested_cleanup()

        assert resource.cleaned is True

    def test_document_cache(self):
        cache = DocumentCache(max_size=3)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)

        assert len(cache._cache) == 3

        cache.get("a")

        cache.put("d", 4)

        assert "b" not in cache._cache
        assert "a" in cache._cache
        assert "d" in cache._cache
        assert len(cache._cache) == 3

    @pytest.mark.asyncio
    async def test_document_cache_cleanup(self):
        cache = DocumentCache(max_size=10)
        for i in range(4):
            cache.put(str(i), i)

        assert len(cache._cache) == 4

        await self.manager._suggested_cleanup()

        assert len(cache._cache) == 2

    @pytest.mark.asyncio
    async def test_global_convenience_functions(self):

        cb = MagicMock()
        add_memory_cleanup_callback(cb)
        assert cb in self.manager._cleanup_callbacks

        remove_memory_cleanup_callback(cb)
        assert cb not in self.manager._cleanup_callbacks

        with patch("app.memory_manager.psutil"):
            stats = get_memory_stats()
            assert isinstance(stats, dict)

        await cleanup_memory()

        await shutdown_memory_manager()
