import asyncio
import pytest
from app.repos.keys import get_key_status_manager

@pytest.mark.asyncio
async def test_thundering_herd_suspension(monkeypatch):
    manager = get_key_status_manager()
    key_hash = "fake_key_hash"
    model_name = "test_model"

    class MockDbQuery:
        call_count = 0
        async def __call__(self, *args, **kwargs):
            MockDbQuery.call_count += 1
            # Mock the return value of SELECT failure_count
            return [{"failure_count": 0}]

    mock_db_query = MockDbQuery()
    monkeypatch.setattr("app.repos.keys.db_query", mock_db_query)

    # Simulate 5 concurrent suspensions
    tasks = [
        manager.suspend_key(key_hash, model_name, "quota", "Test")
        for _ in range(5)
    ]

    await asyncio.gather(*tasks)

    assert mock_db_query.call_count <= 2, f"Expected 2 DB queries, got {mock_db_query.call_count}"
