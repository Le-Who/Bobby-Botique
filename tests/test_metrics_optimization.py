import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import sys
import os

# Ensure app can be imported
sys.path.append(os.getcwd())

# Mock dependencies before import if needed
# But here we just need to mock database calls inside the methods

# We need to handle potential global mocks from other tests (like test_web_security.py)
# by ensuring we have a clean app.database module.

class TestMetricsOptimization(unittest.TestCase):
    def setUp(self):
        # Force reload of app.database to clear mocks from other tests
        import sys
        import importlib

        # Unpatch sys.modules if it was patched globally by other tests
        # This is specific to how test_web_security.py operates
        if isinstance(sys.modules.get('app.database'), MagicMock):
             del sys.modules['app.database']

        # Ensure we have the real module
        if 'app.database' not in sys.modules:
            import app.database
        else:
            importlib.reload(sys.modules['app.database'])

        # Also reload metrics as it imports database
        if 'app.metrics' in sys.modules:
            importlib.reload(sys.modules['app.metrics'])
        else:
            import app.metrics

        self.metrics_module = sys.modules['app.metrics']
        self.db_module = sys.modules['app.database']

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.collector = self.metrics_module.MetricsCollector()

    def tearDown(self):
        self.loop.close()

    def test_save_metrics_batch_insert(self):
        """Test that _save_metrics_to_db uses executemany for error logs"""

        # Mock database manager
        mock_pool = AsyncMock()
        mock_connection = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_connection

        # Use the reloaded module's db_manager
        db_manager_path = 'app.database.db_manager'

        # We need to ensure we patch the db_manager that self.collector uses
        # Since self.collector is from reloaded app.metrics, it uses reloaded app.database

        # Patch the db_manager on the reloaded module instance
        with patch.object(self.db_module, 'db_manager') as mock_db_manager:
            mock_db_manager.pool = mock_pool
            mock_db_manager.execute_many = AsyncMock()
            mock_db_manager.query = AsyncMock()

            mock_execute_many = mock_db_manager.execute_many
            mock_query = mock_db_manager.query

            # Setup initial state
            self.collector.metrics.request_count = 10

            # Add multiple errors
            async def add_errors():
                await self.collector.record_error("TestError1", "Message 1")
                await self.collector.record_error("TestError2", "Message 2")
                await self.collector.record_error("TestError3", "Message 3")

            self.loop.run_until_complete(add_errors())

            # Verify errors are in queue and unsaved
            self.assertEqual(len(self.collector.error_log), 3)
            for error in self.collector.error_log:
                self.assertFalse(error['saved'])

            # Run _save_metrics_to_db
            self.loop.run_until_complete(self.collector._save_metrics_to_db())

            # Verify db_query was called for metrics upsert (once)
            self.assertTrue(mock_query.called)

            # Verify execute_many was called for errors (once)
            self.assertTrue(mock_execute_many.called)

            # Verify arguments passed to execute_many
            args, _ = mock_execute_many.call_args
            query, params_list, retries, conn = args

            self.assertIn("INSERT INTO error_logs", query)
            self.assertEqual(len(params_list), 3)
            # The order depends on deque iteration which is FIFO
            self.assertEqual(params_list[0], ("TestError1", "Message 1"))
            self.assertEqual(params_list[1], ("TestError2", "Message 2"))
            self.assertEqual(params_list[2], ("TestError3", "Message 3"))

            # Verify errors are marked as saved
            for error in self.collector.error_log:
                self.assertTrue(error['saved'])

if __name__ == '__main__':
    unittest.main()
