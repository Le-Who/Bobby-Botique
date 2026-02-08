import unittest
import asyncio
from unittest.mock import MagicMock, patch
import sys
import importlib

class TestRoleConversationMetrics(unittest.TestCase):
    def setUp(self):
        # Create a new event loop for each test
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Mock dependencies that might be missing in the test environment
        self.mock_db = MagicMock()
        self.mock_config = MagicMock()

        # Patch sys.modules to mock app.database and app.config
        # This prevents import errors for missing dependencies (pytz, pydantic)
        # and prevents actual DB connections.
        self.patcher = patch.dict(sys.modules, {
            'app.database': self.mock_db,
            'app.config': self.mock_config
        })
        self.patcher.start()

        # Configure app.config.settings mock
        self.mock_config.settings = MagicMock()

        # Import app.metrics (or reload if already imported)
        # We need to ensure we use the mocked modules
        if 'app.metrics' in sys.modules:
            importlib.reload(sys.modules['app.metrics'])
        else:
            import app.metrics

        self.metrics_module = sys.modules['app.metrics']
        self.collector = self.metrics_module.RoleConversationMetricsCollector()

    def tearDown(self):
        self.patcher.stop()
        self.loop.close()

    def test_initialization(self):
        """Test that metrics start at zero"""
        self.assertEqual(self.collector.role_metrics.custom_roles_created, 0)
        self.assertEqual(self.collector.role_metrics.role_clears, 0)
        self.assertEqual(self.collector.role_metrics.role_saves, 0)
        self.assertEqual(len(self.collector.role_metrics.role_applications), 0)

        self.assertEqual(self.collector.conversation_metrics.conversations_saved, 0)
        self.assertEqual(self.collector.conversation_metrics.conversations_switched, 0)
        self.assertEqual(self.collector.conversation_metrics.conversations_renamed, 0)
        self.assertEqual(self.collector.conversation_metrics.conversations_deleted, 0)
        self.assertEqual(self.collector.conversation_metrics.total_conversations, 0)

        self.assertEqual(self.collector.summarization_metrics.summarizations_triggered, 0)
        self.assertEqual(self.collector.summarization_metrics.summarizations_soft_limit, 0)
        self.assertEqual(self.collector.summarization_metrics.summarizations_hard_limit, 0)
        self.assertEqual(self.collector.summarization_metrics.total_tokens_saved, 0)
        self.assertEqual(self.collector.summarization_metrics.average_summary_length, 0.0)

    def test_role_metrics(self):
        """Test recording role metrics"""
        async def record():
            await self.collector.record_role_application("test_role")
            await self.collector.record_custom_role_creation()
            await self.collector.record_role_clear()
            await self.collector.record_role_save()

        self.loop.run_until_complete(record())

        self.assertEqual(self.collector.role_metrics.role_applications.get("test_role"), 1)
        self.assertEqual(self.collector.role_metrics.custom_roles_created, 1)
        self.assertEqual(self.collector.role_metrics.role_clears, 1)
        self.assertEqual(self.collector.role_metrics.role_saves, 1)

    def test_conversation_metrics(self):
        """Test recording conversation metrics"""
        async def record():
            await self.collector.record_conversation_saved()
            await self.collector.record_conversation_switched()
            await self.collector.record_conversation_renamed()
            await self.collector.record_conversation_deleted()

        self.loop.run_until_complete(record())

        self.assertEqual(self.collector.conversation_metrics.conversations_saved, 1)
        self.assertEqual(self.collector.conversation_metrics.conversations_switched, 1)
        self.assertEqual(self.collector.conversation_metrics.conversations_renamed, 1)
        self.assertEqual(self.collector.conversation_metrics.conversations_deleted, 1)

    def test_summarization_metrics(self):
        """Test recording summarization metrics"""
        async def record():
            await self.collector.record_summarization("мягкий лимит", 100, 50)
            await self.collector.record_summarization("жёсткий лимит", 200, 150)

        self.loop.run_until_complete(record())

        self.assertEqual(self.collector.summarization_metrics.summarizations_triggered, 2)
        self.assertEqual(self.collector.summarization_metrics.summarizations_soft_limit, 1)
        self.assertEqual(self.collector.summarization_metrics.summarizations_hard_limit, 1)
        self.assertEqual(self.collector.summarization_metrics.total_tokens_saved, 300)

        # Average length: (50 + 150) / 2 = 100
        self.assertEqual(self.collector.summarization_metrics.average_summary_length, 100.0)

    def test_get_metrics_summary(self):
        """Test getting metrics summary"""
        async def setup_metrics():
            await self.collector.record_role_application("role1")
            await self.collector.record_conversation_saved()
            await self.collector.record_summarization("test", 100, 50)
            return await self.collector.get_metrics_summary()

        summary = self.loop.run_until_complete(setup_metrics())

        self.assertIn('roles', summary)
        self.assertIn('conversations', summary)
        self.assertIn('summarization', summary)

        self.assertEqual(summary['roles']['applications']['role1'], 1)
        self.assertEqual(summary['conversations']['saved'], 1)
        self.assertEqual(summary['summarization']['triggered'], 1)

    def test_concurrency(self):
        """Test concurrent updates to metrics"""
        async def worker():
            for _ in range(100):
                await self.collector.record_role_application("role_concurrent")

        async def run_concurrent():
            tasks = [worker() for _ in range(5)]
            await asyncio.gather(*tasks)

        self.loop.run_until_complete(run_concurrent())

        self.assertEqual(self.collector.role_metrics.role_applications["role_concurrent"], 500)

if __name__ == '__main__':
    unittest.main()
