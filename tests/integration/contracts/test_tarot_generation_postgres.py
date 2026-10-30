"""Real transaction fencing in the disposable migrated database."""

from datetime import date

import pytest

from app import tarot_daily
from app.repos import tarot_generation as repo

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_stale_generation_cannot_overwrite_or_release_current_owner(contract_pool):
    day = date(2097, 10, 28)
    first = await repo.begin_generation(day, "ru")
    second = await repo.begin_generation(day, "ru")
    data = {"reading_date": day, "card_name": "Маг", "orientation": "Прямая", "language": "ru"}
    await tarot_daily.upsert_prepared_daily_reading(**data, body_markdown="current", ticket=second)
    with pytest.raises(repo.TarotGenerationConflict):
        await tarot_daily.upsert_prepared_daily_reading(**data, body_markdown="stale", ticket=first)
    assert not await repo.finish_generation(first)
    row = await tarot_daily.get_prepared_daily_reading(**data)
    assert row.body_markdown == "current"
    assert await repo.finish_generation(second)
    await tarot_daily.upsert_prepared_daily_reading(**data, body_markdown="late_inline")
    row = await tarot_daily.get_prepared_daily_reading(**data)
    assert row.body_markdown == "current"
