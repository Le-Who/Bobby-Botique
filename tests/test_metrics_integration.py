import asyncio
import os
import sys
import unittest
from datetime import datetime
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
                await self.collector.record_request("test_handler", 0.15, success=True, user_id=42)
                await self.collector.record_api_call("gemini", model="gemini-2.5-flash")
                await self.collector.record_error("TestError", "Simulated Error 1")
                await self.collector.record_error("AuthError", "Simulated Error 2")

                self.assertEqual(self.collector._events_queue.qsize(), 4)

                while not self.collector._events_queue.empty():
                    event = await self.collector._events_queue.get()
                    self.collector._process_event(event)
                    self.collector._events_queue.task_done()

                self.assertEqual(self.collector._events_queue.qsize(), 0)

                self.assertEqual(len(self.collector.daily_metrics), 1)
                self.assertEqual(next(iter(self.collector.daily_metrics.values())).api_calls["gemini"], 1)

                self.assertEqual(len(self.collector.error_log), 2)
                self.assertFalse(self.collector.error_log[0]["saved"])
                self.assertEqual(self.collector.error_log[0]["request_id"], "test_rid_123")

                await self.collector._save_metrics_to_db()

                self.assertTrue(mock_query.called)

                self.assertTrue(mock_execute_many.called)

                args, _ = mock_execute_many.call_args_list[0]
                query, params_list, *rest = args

                self.assertIn("INSERT INTO error_logs", query)
                self.assertEqual(len(params_list), 2)

                self.assertIn("created_at", query)
                for index, (error_type, message) in enumerate(
                    [("TestError", "Simulated Error 1"), ("AuthError", "Simulated Error 2")]
                ):
                    occurred_at = datetime.fromisoformat(self.collector.error_log[index]["timestamp"])
                    self.assertEqual(params_list[index], (error_type, message, "test_rid_123", occurred_at))

                self.assertTrue(self.collector.error_log[0]["saved"])
                self.assertTrue(self.collector.error_log[1]["saved"])

            self.loop.run_until_complete(simulate_events())


if __name__ == "__main__":
    unittest.main()
