"""
Memory management system for optimizing resource usage.
Provides automatic memory cleanup and monitoring.
"""

import gc
import logging
import asyncio
import time
import psutil
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class MemoryThreshold:
    """Memory threshold configuration."""

    warning_percent: float = 70.0
    critical_percent: float = 85.0
    cleanup_percent: float = 60.0


class MemoryManager:
    """Manages memory usage and provides automatic cleanup."""

    def __init__(self, thresholds: Optional[MemoryThreshold] = None):
        self.thresholds = thresholds or MemoryThreshold()
        self._monitoring_task: Optional[asyncio.Task] = None
        self._cleanup_callbacks: List[Callable] = []
        self._memory_history: List[Dict[str, Any]] = []
        self._max_history_size = 100
        self._monitor_interval = 60  # seconds
        self._last_cleanup = time.time()
        self._cleanup_cooldown = 300  # 5 minutes between cleanups
        self._running = False

        try:
            asyncio.get_running_loop()
            self._start_monitoring()
        except RuntimeError:
            logging.debug(
                "No running event loop found, memory monitoring not started automatically."
            )

    def _start_monitoring(self):
        """Starts memory monitoring task."""
        if self._monitoring_task and not self._monitoring_task.done():
            return

        try:
            self._running = True
            self._monitoring_task = asyncio.create_task(self._monitor_memory())
        except RuntimeError as e:
            self._running = False
            logging.error(f"Failed to start memory monitoring: {e}")

    def start(self):
        """Manually starts memory monitoring."""
        self._start_monitoring()

    async def _monitor_memory(self):
        """Continuous memory monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self._monitor_interval)
                await self._check_memory_usage()

                # Clean up old history
                cutoff_time = datetime.now() - timedelta(hours=1)
                self._memory_history = [
                    entry
                    for entry in self._memory_history
                    if entry["timestamp"] > cutoff_time
                ]

                # Keep only recent entries
                if len(self._memory_history) > self._max_history_size:
                    self._memory_history = self._memory_history[
                        -self._max_history_size :
                    ]

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Memory monitoring error: %s", e)
                await asyncio.sleep(60)

    async def _check_memory_usage(self):
        """Checks current memory usage and triggers cleanup if needed."""
        try:
            memory_info = self._get_memory_info()
            if "error" in memory_info:
                logging.warning("Memory info error: %s", memory_info["error"])
                return

            self._memory_history.append(memory_info)

            # Check thresholds
            if memory_info["percent"] >= self.thresholds.critical_percent:
                logging.critical(
                    "Critical memory usage: %.1f%%", memory_info["percent"]
                )
                await self._emergency_cleanup()
            elif memory_info["percent"] >= self.thresholds.warning_percent:
                logging.warning("High memory usage: %.1f%%", memory_info["percent"])
                await self._suggested_cleanup()
            elif memory_info["percent"] <= self.thresholds.cleanup_percent:
                # Memory usage is low, we can be more aggressive with cleanup
                if time.time() - self._last_cleanup > self._cleanup_cooldown:
                    await self._preventive_cleanup()

        except Exception as e:
            logging.error("Memory check error: %s", e)

    def _get_memory_info(self) -> Dict[str, Any]:
        """Gets current memory usage information."""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            virtual_memory = psutil.virtual_memory()

            return {
                "timestamp": datetime.now(),
                "rss_mb": memory_info.rss / 1024 / 1024,  # Resident Set Size in MB
                "vms_mb": memory_info.vms / 1024 / 1024,  # Virtual Memory Size in MB
                "percent": process.memory_percent(),
                "available_mb": virtual_memory.available / 1024 / 1024,
                "total_mb": virtual_memory.total / 1024 / 1024,
                "cpu_percent": process.cpu_percent(interval=0.1),
            }
        except Exception as e:
            logging.error("Failed to get memory info: %s", e)
            return {"timestamp": datetime.now(), "error": str(e)}

    async def _emergency_cleanup(self):
        """Performs emergency memory cleanup."""
        logging.warning("Performing emergency memory cleanup")

        try:
            # Force garbage collection in a separate thread to prevent Event Loop blocking
            def _run_gc():
                return gc.collect()

            collected = await asyncio.to_thread(_run_gc)
            if collected > 0:
                logging.info(
                    "Emergency garbage collection collected %d objects", collected
                )

            # Run all cleanup callbacks
            for callback in self._cleanup_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await asyncio.wait_for(callback(), timeout=5.0)
                    else:
                        callback()
                except asyncio.TimeoutError:
                    logging.warning("Cleanup callback timed out")
                except Exception as e:
                    logging.error("Cleanup callback error: %s", e)

            # Clear memory history to free memory
            self._memory_history.clear()

            self._last_cleanup = time.time()

            return {"status": "cleanup_completed", "timestamp": time.time()}

        except Exception as e:
            logging.error("Emergency cleanup error: %s", e)
            return {"status": "error", "error": str(e)}

    async def force_cleanup(self):
        """Forces memory cleanup (alias for emergency cleanup)."""
        return await self._emergency_cleanup()

    async def _suggested_cleanup(self):
        """Performs suggested memory cleanup."""
        logging.info("Performing suggested memory cleanup")

        try:
            # Run garbage collection in thread
            def _run_gc():
                return gc.collect()

            collected = await asyncio.to_thread(_run_gc)
            if collected > 0:
                logging.info("Garbage collection collected %d objects", collected)

            # Run cleanup callbacks
            for callback in self._cleanup_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await asyncio.wait_for(callback(), timeout=2.0)
                    else:
                        callback()
                except asyncio.TimeoutError:
                    logging.warning("Cleanup callback timed out")
                except Exception as e:
                    logging.error("Cleanup callback error: %s", e)

            self._last_cleanup = time.time()

        except Exception as e:
            logging.error("Suggested cleanup error: %s", e)

    async def _preventive_cleanup(self):
        """Performs preventive memory cleanup."""
        logging.debug("Performing preventive memory cleanup")

        try:
            # Light garbage collection in thread
            def _run_gc():
                return gc.collect(0)  # Only collect youngest generation

            collected = await asyncio.to_thread(_run_gc)
            if collected > 0:
                logging.debug(
                    "Preventive garbage collection collected %d objects", collected
                )

            self._last_cleanup = time.time()

        except Exception as e:
            logging.error("Preventive cleanup error: %s", e)

    def add_cleanup_callback(self, callback: Callable) -> None:
        """Adds a cleanup callback function."""
        self._cleanup_callbacks.append(callback)

    def remove_cleanup_callback(self, callback: Callable) -> None:
        """Removes a cleanup callback function."""
        if callback in self._cleanup_callbacks:
            self._cleanup_callbacks.remove(callback)

    def get_memory_stats(self) -> Dict[str, Any]:
        """Returns current memory statistics."""
        try:
            memory_info = self._get_memory_info()
            if "error" in memory_info:
                return {"error": memory_info["error"]}

            return {
                "current_usage_mb": memory_info["rss_mb"],
                "current_usage_percent": memory_info["percent"],
                "available_mb": memory_info["available_mb"],
                "total_mb": memory_info["total_mb"],
                "cpu_percent": memory_info["cpu_percent"],
                "history_size": len(self._memory_history),
                "last_cleanup": self._last_cleanup,
                "thresholds": {
                    "warning": self.thresholds.warning_percent,
                    "critical": self.thresholds.critical_percent,
                    "cleanup": self.thresholds.cleanup_percent,
                },
            }
        except Exception as e:
            return {"error": str(e)}

    async def stop(self):
        """Stops memory monitoring."""
        self._running = False
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        # Final cleanup
        self._cleanup_callbacks.clear()
        self._memory_history.clear()
        logging.info("Memory manager stopped")

    async def shutdown(self):
        """Shuts down the memory manager (alias for stop)."""
        await self.stop()

    def __del__(self):
        """Cleanup on destruction."""
        if self._running:
            logging.warning("Memory manager destroyed while running")
            # Note: We can't await here, but we can try to cancel the task
            if self._monitoring_task and not self._monitoring_task.done():
                self._monitoring_task.cancel()


# Global memory manager instance
memory_manager = MemoryManager()


# Convenience functions
async def cleanup_memory() -> Dict[str, Any]:
    """Forces memory cleanup."""
    return await memory_manager.force_cleanup()


def get_memory_stats() -> Dict[str, Any]:
    """Gets memory statistics."""
    return memory_manager.get_memory_stats()


def add_memory_cleanup_callback(callback: Callable) -> None:
    """Adds a memory cleanup callback."""
    memory_manager.add_cleanup_callback(callback)


def remove_memory_cleanup_callback(callback: Callable) -> None:
    """Removes a memory cleanup callback."""
    memory_manager.remove_cleanup_callback(callback)


async def shutdown_memory_manager():
    """Shuts down the memory manager."""
    await memory_manager.shutdown()


# Automatic cleanup for common resources
class AutoCleanupResource:
    """Base class for resources that need automatic cleanup."""

    def __init__(self, name: str):
        self.name = name
        self._cleanup_registered = False
        self._register_cleanup()

    def _register_cleanup(self):
        """Registers this resource for automatic cleanup."""
        if not self._cleanup_registered:
            memory_manager.add_cleanup_callback(self._cleanup)
            self._cleanup_registered = True

    def _cleanup(self):
        """Performs cleanup of this resource."""
        try:
            self._perform_cleanup()
        except Exception as e:
            logging.error("Auto-cleanup error for %s: %s", self.name, e)

    def _perform_cleanup(self):
        """Override this method to implement specific cleanup logic."""
        pass

    def __del__(self):
        """Ensures cleanup callback is removed."""
        if self._cleanup_registered:
            try:
                memory_manager.remove_cleanup_callback(self._cleanup)
            except:
                pass


# Example usage for document processing
class DocumentCache(AutoCleanupResource):
    """Document cache with automatic memory cleanup."""

    def __init__(self, max_size: int = 100):
        super().__init__("DocumentCache")
        self.max_size = max_size
        self._cache = {}
        self._access_times = {}

    def get(self, key: str):
        """Gets a document from cache."""
        if key in self._cache:
            self._access_times[key] = time.time()
            return self._cache[key]
        return None

    def put(self, key: str, value: Any):
        """Puts a document in cache."""
        if len(self._cache) >= self.max_size:
            self._evict_oldest()

        self._cache[key] = value
        self._access_times[key] = time.time()

    def _evict_oldest(self):
        """Evicts the oldest accessed document."""
        if not self._access_times:
            return

        oldest_key = min(self._access_times.keys(), key=lambda k: self._access_times[k])
        del self._cache[oldest_key]
        del self._access_times[oldest_key]

    def _perform_cleanup(self):
        """Performs cleanup when memory is low."""
        # Clear half of the cache
        items_to_remove = len(self._cache) // 2
        for _ in range(items_to_remove):
            self._evict_oldest()

        logging.info("DocumentCache cleaned up %d items", items_to_remove)
