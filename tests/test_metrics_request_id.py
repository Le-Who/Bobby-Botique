import asyncio
import unittest
import sys
import os

sys.path.append(os.getcwd())
from unittest.mock import AsyncMock, patch

from app.metrics import MetricsCollector


class TestMetricsRequestId(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.collector = MetricsCollector()

    def tearDown(self):
        self.loop.close()

    def test_record_error_stores_request_id(self):
        async def run_case():
            with patch('app.metrics.get_request_id', return_value='rid-metrics'):
                await self.collector.record_error('test_error', 'boom')

        self.loop.run_until_complete(run_case())
        self.assertEqual(self.collector.error_log[-1]['request_id'], 'rid-metrics')

    def test_record_api_call_stores_request_id_in_events(self):
        async def run_case():
            with patch('app.metrics.get_request_id', return_value='rid-api'):
                await self.collector.record_api_call('gemini', model='gemini-2.5-pro')

        self.loop.run_until_complete(run_case())
        self.assertEqual(self.collector.api_event_log[-1]['request_id'], 'rid-api')

    def test_save_metrics_writes_request_id_to_db(self):
        async def run_case():
            await self.collector.record_error('TypeA', 'MessageA', request_id='rid-db')
            with patch('app.metrics.db.db_query', AsyncMock(return_value=[])), patch(
                'app.metrics.db.db_execute_many', AsyncMock(return_value=None)
            ) as mock_exec_many:
                await self.collector._save_metrics_to_db()
                args = mock_exec_many.call_args[0]
                self.assertIn('request_id', args[0])
                self.assertEqual(args[1][0], ('TypeA', 'MessageA', 'rid-db'))

        self.loop.run_until_complete(run_case())


if __name__ == '__main__':
    unittest.main()
