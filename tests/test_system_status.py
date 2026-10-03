import unittest
from unittest.mock import AsyncMock, patch

from app import metrics


class TestSystemStatus(unittest.IsolatedAsyncioTestCase):
    @patch("app.metrics.metrics_collector.get_metrics_summary", new_callable=AsyncMock)
    @patch("app.utils.time.get_pacific_date")
    @patch("app.utils.time.get_current_month_str")
    @patch("app.utils.time.get_kyiv_reset_time")
    async def test_get_system_status_data(
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
        mock_gemini_usage = [{"key_hash": "hash1", "model_name": "gemini-pro", "request_count": 10}]
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

        # Scope database calls on the real metrics module
        with patch.object(metrics, "db") as mock_db:
            mock_db.db_query = mock_db_query

            # Run function
            result = await metrics.get_system_status_data()

        # Assertions
        self.assertEqual(result["metrics_summary"], mock_metrics_summary)

        # Keys are masked by _mask_key() for security — short keys (< 10 chars) become "****"
        self.assertEqual(result["gemini"]["keys"][0]["key_hash"], "hash1")
        self.assertEqual(result["gemini"]["keys"][0]["api_key"], "****")

        self.assertEqual(result["tavily"]["keys"][0]["key_hash"], "thash1")
        self.assertEqual(result["tavily"]["keys"][0]["api_key"], "****")


if __name__ == "__main__":
    unittest.main()
