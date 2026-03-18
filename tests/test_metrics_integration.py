import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.getcwd())


class TestMetricsIntegration(unittest.TestCase):
    def setUp(self):
        import app.database as db_module
        import app.metrics as metrics_module

        self.metrics_module = metrics_module
        self.db_module = db_module

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.collector = self.metrics_module.MetricsCollector()

    def tearDown(self):
        self.loop.close()

    def test_metrics_queue_and_batch_save(self):
        """Test that asynchronous recording methods place items uniformly in the queue
        and that _save_metrics_to_db performs batched inserts."""

        # Mock the db manager dependencies
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("app.metrics.db.db_query", new_callable=AsyncMock) as mock_query,
            patch("app.metrics.db.db_execute_many", new_callable=AsyncMock) as mock_execute_many,
            patch("app.metrics.db.db_manager") as mock_db_manager,
            patch("app.metrics.get_request_id", return_value="test_rid_123"),
        ):
            mock_db_manager.pool = mock_pool
            mock_db_manager.is_connected = True

            async def simulate_events():
                # 1. Record API Call
                await self.collector.record_api_call("gemini", model="gemini-2.5-flash")
                # 2. Record Error (1)
                await self.collector.record_error("TestError", "Simulated Error 1")
                # 3. Record Error (2)
                await self.collector.record_error("AuthError", "Simulated Error 2")

                # Check that queue has 3 items
                self.assertEqual(self.collector._events_queue.qsize(), 3)

                # Drain the queue manually (simulate the background worker)
                while not self.collector._events_queue.empty():
                    event = await self.collector._events_queue.get()
                    self.collector._process_event(event)
                    self.collector._events_queue.task_done()

                self.assertEqual(self.collector._events_queue.qsize(), 0)

                # Validate dictionaries have been updated properly
                today = self.metrics_module.date.today().isoformat()
                self.assertEqual(self.collector.daily_metrics.get(today).api_calls["gemini"], 1)

                # We expect 2 items in the error log to be saved
                self.assertEqual(len(self.collector.error_log), 2)
                self.assertFalse(self.collector.error_log[0]["saved"])
                self.assertEqual(self.collector.error_log[0]["request_id"], "test_rid_123")

                # 4. Trigger Batch Save
                await self.collector._save_metrics_to_db()

                # Verify that db_query was called for daily aggregations (UPSERT logic)
                self.assertTrue(mock_query.called)

                # Verify that execute_many was called for batched errors
                self.assertTrue(mock_execute_many.called)

                args, _ = mock_execute_many.call_args
                query, params_list, *rest = args

                self.assertIn("INSERT INTO error_logs", query)
                self.assertEqual(len(params_list), 2)

                # Validate the contents of the parameter list matches the error logs
                # params structure: (type, message, request_id)
                self.assertEqual(params_list[0], ("TestError", "Simulated Error 1", "test_rid_123"))
                self.assertEqual(params_list[1], ("AuthError", "Simulated Error 2", "test_rid_123"))

                # Verify items are marked as saved
                self.assertTrue(self.collector.error_log[0]["saved"])
                self.assertTrue(self.collector.error_log[1]["saved"])

            self.loop.run_until_complete(simulate_events())


if __name__ == "__main__":
    unittest.main()
