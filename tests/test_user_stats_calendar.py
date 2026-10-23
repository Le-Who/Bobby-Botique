"""Stats readers use the same Kyiv day as recorded metric events."""

from datetime import UTC, date, datetime

import pytest

from app.repos import user_stats


class MidnightClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 23, 23, 30, tzinfo=UTC).astimezone(tz)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader", ["get_user_today_request_count", "get_user_model_usage_today", "get_user_weekly_stats"]
)
async def test_stats_bind_kyiv_day_when_utc_calendar_is_still_yesterday(monkeypatch, reader):
    monkeypatch.setattr(user_stats, "datetime", MidnightClock, raising=False)

    async def query(_sql, params):
        if reader == "get_user_weekly_stats":
            assert params == (42, date(2026, 10, 18), date(2026, 10, 24))
            return [{"metric_date": date(2026, 10, 24), "cnt": 11}]
        assert params == (42, date(2026, 10, 24))
        return [{"cnt": 11}] if reader == "get_user_today_request_count" else [{"model_name": "synthetic", "cnt": 11}]

    monkeypatch.setattr(user_stats.db, "db_query", query)
    result = await getattr(user_stats, reader)(42)
    if reader == "get_user_today_request_count":
        assert result == 11
    elif reader == "get_user_model_usage_today":
        assert result == [{"model_name": "synthetic", "cnt": 11}]
    else:
        assert result == [{"metric_date": date(2026, 10, 24), "cnt": 11}]
