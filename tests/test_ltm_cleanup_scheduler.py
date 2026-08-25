"""Periodic LTM retention cleanup integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_ltm_cleanup_job_runs_retention_cleanup():
    from bot import cleanup_ltm_job

    with patch(
        "app.repos.memory.cleanup_expired_memories",
        new=AsyncMock(return_value=3),
    ) as cleanup:
        await cleanup_ltm_job(MagicMock())

    cleanup.assert_awaited_once_with()
