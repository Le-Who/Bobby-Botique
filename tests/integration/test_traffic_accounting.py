"""Exercise owned traffic SQL and repeatable high water writes on disposable PG."""

from datetime import UTC, datetime

import pytest

from app import metrics
from app.metrics import MetricsCollector

pytestmark = pytest.mark.integration


async def test_traffic_round_trip_two_processes_and_restart(db_conn_with_user, monkeypatch):
    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    await db_conn_with_user.execute(
        "INSERT INTO traffic_accounting_epoch (singleton, started_at) VALUES(TRUE, $1)",
        datetime(2026, 10, 24, 12, tzinfo=UTC),
    )
    await db_conn_with_user.execute(
        "INSERT INTO user_metrics (user_id,metric_date,request_count) VALUES(999999,DATE '2026-10-25',4)"
    )
    monkeypatch.setattr(type(metrics.db.db_manager), "is_connected", property(lambda self: True))
    first, second = MetricsCollector(), MetricsCollector()
    first._api_daily["2026-10-25"] = 3
    second._api_daily["2026-10-25"] = 2
    for _ in range(2):
        await first._save_traffic_to_db()
        await second._save_traffic_to_db()
    # Replay the same high water after a lost local acknowledgement.
    first._api_flushed.clear()
    await first._save_traffic_to_db()
    first._api_daily["2026-10-25"] += 1
    traffic = await first.get_traffic_summary(now=now)
    assert traffic["periods"]["today"]["api_attempts"] == 6
    assert traffic["periods"]["today"]["user_requests"] == 4
    assert traffic["periods"]["today"]["complete"] is True
    assert traffic["periods"]["last_7_days"]["coverage"] == "partial"
    await first._save_traffic_to_db()
    restarted = MetricsCollector()
    traffic = await restarted.get_traffic_summary(now=now)
    assert traffic["periods"]["today"]["api_attempts"] == 6
