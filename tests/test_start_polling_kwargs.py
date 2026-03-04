"""Regression test: verify start_polling() is called with only valid v22+ kwargs.

python-telegram-bot v22.0 removed read_timeout, write_timeout,
connect_timeout, pool_timeout from Updater.start_polling().
This test uses AST inspection to catch regressions statically.
"""

import ast
import inspect

from telegram.ext import Updater


def test_start_polling_kwargs_are_valid():
    """Parse bot.py AST and check that start_polling() only uses v22+ params."""
    # Get valid parameters from the installed version
    valid_params = set(inspect.signature(Updater.start_polling).parameters.keys())
    valid_params.discard("self")

    # Params that were removed in v22.0 and must NOT appear
    removed_v22 = {"read_timeout", "write_timeout", "connect_timeout", "pool_timeout"}

    # Parse bot.py source
    with open("bot.py", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename="bot.py")

    # Find all calls to start_polling
    found_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "start_polling":
                kwargs_used = {kw.arg for kw in node.keywords if kw.arg is not None}
                found_calls.append((node.lineno, kwargs_used))

    assert found_calls, "No start_polling() call found in bot.py — suspicious"

    for lineno, kwargs_used in found_calls:
        # Check no removed kwargs
        invalid_removed = kwargs_used & removed_v22
        assert not invalid_removed, (
            f"bot.py:{lineno} — start_polling() uses removed kwargs: {invalid_removed}. "
            f"These were removed in python-telegram-bot v22.0."
        )

        # Check all kwargs are valid
        unknown = kwargs_used - valid_params
        assert not unknown, (
            f"bot.py:{lineno} — start_polling() uses unknown kwargs: {unknown}. "
            f"Valid params in installed version: {valid_params}"
        )
