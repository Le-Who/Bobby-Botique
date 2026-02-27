import logging
import httpx
from typing import Dict, Any, Optional
import asyncio

from app.config import settings
from app.repos.keys import get_available_tavily_key, increment_tavily_key_usage
from app.metrics import metrics_collector
from app.cache import get_cached_search_result, cache_search_result
from app.request_context import get_request_id
from app.utils.network import NetworkErrorHandler
from app.utils.api_logger import api_logger
from app.resilience_policy import run_with_resilience
from app.circuit_breaker import TAVILY_API_CONFIG

# Robust HTTP client for Tavily API calls
http_client = NetworkErrorHandler.create_robust_http_client()






async def _tavily_api_call(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Internal function for making Tavily API calls with circuit breaker."""
    async def _do_call():
        headers = {}
        request_id = get_request_id()
        if request_id:
            headers["X-Request-ID"] = request_id

        response = await http_client.post(
            "https://api.tavily.com/search", json=payload, headers=headers or None
        )
        response.raise_for_status()
        return response.json()

    result, attempts = await run_with_resilience(
        _do_call,
        circuit_name="tavily",
    )
    if attempts > 1:
        logging.info("Tavily API call succeeded after %d attempts", attempts)
    return result


async def tavily_search_agent(
    query: str, search_type: str = "search", user_id: int = None, chat_id: int = None
):
    # Input validation
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must be a non-empty string")

    if query and len(query) > 1000:
        raise ValueError("Query too long. Maximum 1000 characters allowed")

    if search_type not in ["search", "qna"]:
        raise ValueError("search_type must be 'search' or 'qna'")

    if user_id is not None and (not isinstance(user_id, int) or user_id <= 0):
        raise ValueError("user_id must be a positive integer")

    if chat_id is not None and not isinstance(chat_id, int):
        raise ValueError("chat_id must be an integer")

    # Check cache before performing search
    cached_result = await get_cached_search_result(query, search_type)
    if cached_result:
        logging.info("Cache hit for Tavily search: %s...", query[:50])
        return cached_result

    available_key = await get_available_tavily_key()
    if not available_key:
        return {
            "error": "Поиск недоступен: все API ключи сервиса поиска достигли месячного лимита."
        }

    api_key = available_key["api_key"]

    # Detailed Tavily API request logging
    start_time = api_logger.log_tavily_request(
        query=query, search_type=search_type, user_id=user_id, chat_id=chat_id
    )

    logging.info(
        f"Performing Tavily API call (type: {search_type}) for query: {query[:100]}"
    )

    # Record search metrics
    await metrics_collector.record_search_query()
    await metrics_collector.record_api_call("tavily", search_type)

    payload = {"api_key": api_key, "query": query}
    cost = 0

    if search_type == "qna":
        payload["search_depth"] = "basic"
        cost = settings.TAVILY_QNA_SEARCH_COST
    else:
        payload["search_depth"] = "advanced"
        payload["max_results"] = 7
        cost = settings.TAVILY_ADVANCED_SEARCH_COST

    try:
        data = await _tavily_api_call(payload)
        await increment_tavily_key_usage(available_key["key_hash"], cost)

        result = {}
        if search_type == "qna":
            result = {"type": "answer", "content": data.get("answer", "")}
        else:
            result = {"type": "search", "results": data.get("results", [])}

        # Cache the result
        await cache_search_result(query, search_type, result)

        # Log successful Tavily API response
        results = result.get("results", [])
        results_count = (
            len(results) if results and result.get("type") == "search" else 1
        )
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=results_count,
            success=True,
            user_id=user_id,
            chat_id=chat_id,
        )

        return result

    except httpx.HTTPStatusError as e:
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=0,
            success=False,
            error_message=f"HTTP {e.response.status_code}: {e.response.text}",
            user_id=user_id,
            chat_id=chat_id,
        )

        logging.error(
            f"Tavily API call failed with status {e.response.status_code}: {e.response.text}"
        )
        await metrics_collector.record_error(
            "tavily_http", f"Status {e.response.status_code}: {e.response.text}"
        )
        return {
            "error": f"Ошибка API поиска: {e.response.status_code}. Убедитесь, что ключ API валиден."
        }
    except Exception as e:
        api_logger.log_tavily_response(
            start_time=start_time,
            search_type=search_type,
            results_count=0,
            success=False,
            error_message=str(e),
            user_id=user_id,
            chat_id=chat_id,
        )

        logging.error("Tavily API call failed: %s", e, exc_info=True)
        await metrics_collector.record_error("tavily_api", str(e))
        return {"error": f"Произошла непредвиденная ошибка API: {e}"}
