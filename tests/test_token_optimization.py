import sys
import unittest
import types
from unittest.mock import MagicMock

# Mock app.config
config_mock = types.ModuleType("app.config")
settings_mock = MagicMock()
settings_mock.DEFAULT_SYSTEM_PROMPT = "System Prompt"
settings_mock.COMPACT_SYSTEM_PROMPT = "Compact Prompt"
config_mock.settings = settings_mock
sys.modules["app.config"] = config_mock

# Mock app.metrics
metrics_mock = types.ModuleType("app.metrics")
metrics_mock.role_conv_metrics = MagicMock()
sys.modules["app.metrics"] = metrics_mock

from app import prompts

class TestTokenOptimization(unittest.TestCase):
    def test_should_summarize_context_optimized(self):
        # Create a large fake history but FEWER messages to avoid message limit
        # 10 messages * 1000 chars = 10000 chars = ~2500 tokens.
        # This is well below 300,000 token limit.
        history = [{'role': 'user', 'parts': ['a' * 1000]}] * 10

        # Limit is 300,000 (soft)

        # Case 1: Without optimization (known_history_tokens=None)
        # Should rely on calculation (~2500 tokens) -> False
        should_sum, reason = prompts.should_summarize_context(history, current_tokens=0)
        self.assertFalse(should_sum, f"Should not summarize with actual tokens ~2500. Reason: {reason}")

        # Case 2: With optimization. Pass a huge number (known_history_tokens=900000)
        # 900000 > HARD_TOKEN_LIMIT (800000) -> True
        known_tokens = 900000
        should_sum, reason = prompts.should_summarize_context(history, current_tokens=0, known_history_tokens=known_tokens)
        self.assertTrue(should_sum, "Should summarize because known_tokens > limit")
        self.assertIn("Превышен жёсткий лимит токенов", reason)

        # Case 3: With optimization. Pass a small number (known_history_tokens=100)
        # 100 < limits -> False
        known_tokens = 100
        should_sum, reason = prompts.should_summarize_context(history, current_tokens=0, known_history_tokens=known_tokens)
        self.assertFalse(should_sum, "Should not summarize because known_tokens is small")

    def test_prepare_context_with_limits_passes_known_tokens(self):
         # Mock should_summarize_context to verify arguments are passed correctly
         # We need to access the function in the module where it is defined
         original = prompts.should_summarize_context
         calls = []
         def mock_should_summarize(history, current_tokens, known_history_tokens=None):
             calls.append(known_history_tokens)
             return False, ""

         prompts.should_summarize_context = mock_should_summarize
         try:
             # Pass NON-EMPTY history to bypass early return
             history = [{'role': 'user', 'parts': ['test']}]
             prompts.prepare_context_with_limits(history, "", known_history_tokens=12345)
             self.assertEqual(len(calls), 1)
             self.assertEqual(calls[0], 12345)
         finally:
             prompts.should_summarize_context = original

if __name__ == '__main__':
    unittest.main()
