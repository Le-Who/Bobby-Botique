import logging

import pytest

from app.metrics import RoleConversationMetricsCollector


@pytest.mark.asyncio
async def test_record_custom_role_creation(caplog):
    collector = RoleConversationMetricsCollector()

    assert collector.role_metrics.custom_roles_created == 0

    with caplog.at_level(logging.INFO):
        await collector.record_custom_role_creation()

    assert collector.role_metrics.custom_roles_created == 1
    assert "Custom role created" in caplog.text

    await collector.record_custom_role_creation()

    assert collector.role_metrics.custom_roles_created == 2
