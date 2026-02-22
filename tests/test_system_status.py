import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import sys
import os

# Ensure app can be imported
sys.path.append(os.getcwd())


class TestSystemStatus(unittest.TestCase):
    def setUp(self):
        import sys
        import importlib

        self.mock_config = MagicMock()
        self.mock_database = MagicMock()
        self.mock_time_utils = MagicMock()
        self.mock_utils = MagicMock()
        self.mock_utils.time = self.mock_time_utils

        self.patcher = patch.dict(
            "sys.modules",
            {
                "app.config": self.mock_config,
                "app.database": self.mock_database,
                "app.utils.time": self.mock_time_utils,
                "app.utils": self.mock_utils,
            },
        )
        self.patcher.start()

        # Reload relevant modules to ensure clean state and usage of mocks
        if "app.metrics" in sys.modules:
            importlib.reload(sys.modules["app.metrics"])
        else:
            import app.metrics  # noqa: F401

        self.metrics_module = sys.modules["app.metrics"]

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        self.patcher.stop()

    @patch("app.metrics.metrics_collector.get_metrics_summary", new_callable=AsyncMock)
    @patch("app.utils.time.get_pacific_date")
    @patch("app.utils.time.get_current_month_str")
    @patch("app.utils.time.get_kyiv_reset_time")
    def test_get_system_status_data(
        self,
        mock_reset_time,
        mock_current_month,
        mock_pacific_date,
        mock_get_metrics,
    ):
        # Setup mocks
        mock_pacific_date.return_value = "2023-10-27"
        mock_current_month.return_value = "2023-10"
        mock_reset_time.return_value = "10:00 28.10.2023"

        mock_metrics_summary = {"total_requests": 100, "error_rate": 1.5}
        mock_get_metrics.return_value = mock_metrics_summary

        # Setup DB responses
        mock_gemini_keys = [{"api_key": "key1", "key_hash": "hash1"}]
        mock_gemini_usage = [
            {"key_hash": "hash1", "model_name": "gemini-pro", "request_count": 10}
        ]
        mock_tavily_keys = [{"api_key": "tav1", "key_hash": "thash1"}]
        mock_tavily_usage = [{"key_hash": "thash1", "credit_usage": 5}]

        async def db_side_effect(query, params=None):
            if "FROM api_keys" in query:
                return mock_gemini_keys
            elif "FROM key_usage" in query:
                return mock_gemini_usage
            elif "FROM tavily_api_keys" in query:
                return mock_tavily_keys
            elif "FROM tavily_key_usage" in query:
                return mock_tavily_usage
            return []

        mock_db_query = AsyncMock(side_effect=db_side_effect)

        # Patch db on the reloaded metrics module directly
        with patch.object(self.metrics_module, "db") as mock_db:
            mock_db.db_query = mock_db_query

            # Run function
            result = self.loop.run_until_complete(
                self.metrics_module.get_system_status_data()
            )

        # Assertions
        self.assertEqual(result["metrics_summary"], mock_metrics_summary)

        self.assertEqual(result["gemini"]["keys"], mock_gemini_keys)
        self.assertEqual(result["gemini"]["usage_map"]["hash1"][0]["request_count"], 10)
        self.assertEqual(result["gemini"]["reset_time"], "10:00 28.10.2023")

        self.assertEqual(result["tavily"]["keys"], mock_tavily_keys)
        self.assertEqual(result["tavily"]["usage_map"]["thash1"], 5)


if __name__ == "__main__":
    unittest.main()
