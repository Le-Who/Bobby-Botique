"""Search requests keep their result budget and tolerate malformed upstream rows."""

from unittest.mock import AsyncMock

import pytest

from app import cache, search_services


@pytest.fixture
def search_boundary(monkeypatch):
    monkeypatch.setattr(cache, "multi_layer_cache", cache.MultiLayerCache())
    monkeypatch.setattr(cache, "redis_client", None)
    monkeypatch.setattr(
        search_services,
        "get_available_tavily_key",
        AsyncMock(return_value={"api_key": "fake", "key_hash": "fake-hash"}),
    )
    monkeypatch.setattr(search_services, "increment_tavily_key_usage", AsyncMock())
    for name in ("record_search_query", "record_api_call", "record_cache_hit", "record_cache_miss"):
        monkeypatch.setattr(search_services.metrics_collector, name, AsyncMock())


@pytest.mark.parametrize("layer", ["memory", "redis"])
async def test_search_cache_preserves_each_requested_result_limit(search_boundary, monkeypatch, layer):
    stored = {}
    if layer == "redis":

        async def get(key):
            return stored.get(key)

        async def setex(key, ttl, value):
            stored[key] = value

        redis = AsyncMock()
        redis.get.side_effect = get
        redis.setex.side_effect = setex
        monkeypatch.setattr(cache, "redis_client", redis)

    requests = []

    async def api_call(payload):
        requests.append(payload["max_results"])
        return {"results": [{"url": f"https://example.test/{i}"} for i in range(payload["max_results"])]}

    monkeypatch.setattr(search_services, "_tavily_api_call", api_call)
    for limit in (1, 3, 1):
        if layer == "redis":
            cache.multi_layer_cache.search_cache.clear()
        result = await search_services.tavily_search_agent("cache budget", max_results=limit)
        assert len(result["results"]) == limit
    assert requests == [1, 3]


@pytest.mark.parametrize("limit", [0, -1, True, "3", None])
async def test_invalid_search_limit_is_rejected_before_external_work(search_boundary, monkeypatch, limit):
    api_call = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(search_services, "_tavily_api_call", api_call)
    with pytest.raises(ValueError, match="max_results"):
        await search_services.tavily_search_agent("invalid budget", max_results=limit)
    api_call.assert_not_awaited()
    search_services.get_available_tavily_key.assert_not_awaited()


async def test_parallel_search_deduplicates_requests_before_external_work(monkeypatch):
    calls = []

    async def search(query, **kwargs):
        calls.append(query)
        return {"results": [{"url": f"https://example.test/{query}", "score": 0.8}]}

    monkeypatch.setattr(search_services, "tavily_search_agent", search)
    result = await search_services.parallel_search(["first", "first", "second", "first"])
    assert calls == ["first", "second"]
    assert [row["url"] for row in result] == ["https://example.test/first", "https://example.test/second"]


async def test_zero_parallel_result_budget_avoids_searches(monkeypatch):
    search = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(search_services, "tavily_search_agent", search)
    assert await search_services.parallel_search(["unused"], max_results=0) == []
    search.assert_not_awaited()


async def test_malformed_queries_do_not_discard_valid_search(search_boundary, monkeypatch):
    api_call = AsyncMock(return_value={"results": [{"url": "https://example.test/good", "score": 0.8}]})
    monkeypatch.setattr(search_services, "_tavily_api_call", api_call)
    result = await search_services.parallel_search([[], {}, None, "valid", "valid", "   "])
    assert [row["url"] for row in result] == ["https://example.test/good"]
    assert api_call.await_count == 1
    assert api_call.await_args.args[0]["query"] == "valid"


@pytest.mark.parametrize("bad_score", [None, "not-a-score", [], float("nan"), float("inf")])
async def test_malformed_search_score_does_not_discard_other_results(monkeypatch, bad_score):
    search = AsyncMock(
        return_value={
            "results": [
                {"url": "https://example.test/malformed", "score": bad_score},
                {"url": "https://example.test/relevant", "score": 0.9},
            ]
        }
    )
    monkeypatch.setattr(search_services, "tavily_search_agent", search)
    result = await search_services.parallel_search(["query"], max_results=1)
    assert result == [
        {"url": "https://example.test/relevant", "title": "", "snippet": "", "score": 0.9, "published_date": ""}
    ]


async def test_malformed_search_result_list_does_not_discard_successful_query(monkeypatch):
    search = AsyncMock(
        side_effect=[{"results": None}, {"results": [{"url": "https://example.test/good", "score": 0.9}]}]
    )
    monkeypatch.setattr(search_services, "tavily_search_agent", search)
    result = await search_services.parallel_search(["malformed", "valid"])
    assert [row["url"] for row in result] == ["https://example.test/good"]
