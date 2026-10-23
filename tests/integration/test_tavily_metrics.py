"""Real PostgreSQL parameter binding for fractional Tavily admission thresholds."""

from types import SimpleNamespace

import pytest

from app.repos import keys, metrics_repo

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.usefixtures("force_test_db_conn")]


@pytest.mark.parametrize(
    "limit,used,allowed",
    [(1000, 0, True), (1000, 969, True), (1000, 970, False), (1000, 1000, False), (0, 0, False)],
)
async def test_tavily_diagnostics_matches_real_monthly_admission(db_conn, monkeypatch, limit, used, allowed):
    # Temporary names shadow only this connection's unqualified key tables.
    # No seed keys or persistent rows are modified.
    await db_conn.execute("CREATE TEMP TABLE tavily_api_keys (key_hash TEXT PRIMARY KEY, api_key TEXT NOT NULL)")
    await db_conn.execute("CREATE TEMP TABLE tavily_key_usage (key_hash TEXT, usage_month TEXT, credit_usage INTEGER)")
    manager = keys.MonthlyKeyManager("tavily_api_keys", "tavily_key_usage", limit, 0.97)
    monkeypatch.setattr(keys, "_tavily_km", manager)
    monkeypatch.setattr(keys, "safe_decrypt", lambda value: value)
    monkeypatch.setattr(
        metrics_repo,
        "settings",
        SimpleNamespace(TAVILY_MONTHLY_CREDIT_LIMIT=limit, TAVILY_LIMIT_THRESHOLD_PERCENT=0.97),
    )
    await db_conn.execute("INSERT INTO tavily_api_keys VALUES ('synthetic-hash', 'synthetic-key')")
    await db_conn.execute(
        "INSERT INTO tavily_key_usage VALUES ('synthetic-hash', $1, $2)", manager._current_month(), used
    )

    row = (await metrics_repo.get_tavily_key_usage_stats())[0]
    selected = await keys.get_available_tavily_key()

    assert row["credit_usage"] == used
    assert row["credit_limit"] == limit
    assert row["admission_limit"] == limit * 0.97
    assert row["is_available"] is allowed
    assert (selected is not None) is allowed
    assert row["usage_percent"] == (used / limit * 100 if limit else None)
