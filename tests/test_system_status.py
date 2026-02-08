import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import sys
import os

# Ensure app can be imported
sys.path.append(os.getcwd())

class TestSystemStatus(unittest.TestCase):
    def setUp(self):
        # We need to ensure clean imports because of the singleton nature of some modules
        import sys
        import importlib
        from unittest.mock import MagicMock

        # Mock dependencies that cause import errors in this environment
        sys.modules['app.config'] = MagicMock()
        sys.modules['app.database'] = MagicMock()

        # app.utils.time also imports pytz, so mock it
        mock_time_utils = MagicMock()
        sys.modules['app.utils.time'] = mock_time_utils
        # Ensure parent package exists
        sys.modules['app.utils'] = MagicMock()
        sys.modules['app.utils'].time = mock_time_utils

        # Reload relevant modules to ensure clean state and usage of mocks
        if 'app.metrics' in sys.modules:
            importlib.reload(sys.modules['app.metrics'])
        else:
            import app.metrics

        self.metrics_module = sys.modules['app.metrics']

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    @patch('app.database.db_query', new_callable=AsyncMock)
    @patch('app.metrics.metrics_collector.get_metrics_summary', new_callable=AsyncMock)
    @patch('app.utils.time.get_pacific_date')
    @patch('app.utils.time.get_current_month_str')
    @patch('app.utils.time.get_kyiv_reset_time')
    def test_get_system_status_data(self, mock_reset_time, mock_current_month, mock_pacific_date, mock_get_metrics, mock_db_query):
        # Setup mocks
        mock_pacific_date.return_value = "2023-10-27"
        mock_current_month.return_value = "2023-10"
        mock_reset_time.return_value = "10:00 28.10.2023"

        mock_metrics_summary = {
            'total_requests': 100,
            'error_rate': 1.5
        }
        mock_get_metrics.return_value = mock_metrics_summary

        # Setup DB responses
        mock_gemini_keys = [{'api_key': 'key1', 'key_hash': 'hash1'}]
        mock_gemini_usage = [{'key_hash': 'hash1', 'model_name': 'gemini-pro', 'request_count': 10}]
        mock_tavily_keys = [{'api_key': 'tav1', 'key_hash': 'thash1'}]
        mock_tavily_usage = [{'key_hash': 'thash1', 'credit_usage': 5}]

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

        mock_db_query.side_effect = db_side_effect

        # Run function
        result = self.loop.run_until_complete(self.metrics_module.get_system_status_data())

        # Assertions
        self.assertEqual(result['metrics_summary'], mock_metrics_summary)

        self.assertEqual(result['gemini']['keys'], mock_gemini_keys)
        self.assertEqual(result['gemini']['usage_map']['hash1'][0]['request_count'], 10)
        self.assertEqual(result['gemini']['reset_time'], "10:00 28.10.2023")

        self.assertEqual(result['tavily']['keys'], mock_tavily_keys)
        self.assertEqual(result['tavily']['usage_map']['thash1'], 5)

if __name__ == '__main__':
    unittest.main()
