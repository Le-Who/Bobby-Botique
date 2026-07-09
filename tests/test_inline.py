import asyncio

import pytest

from app.errors import ErrorCode, tag_error
from app.handlers.inline import parse_inline_query


def test_parse_inline_query_empty():
    assert parse_inline_query("") == {}


def test_parse_inline_query_basic():
    res = parse_inline_query("hello world")
    assert res["is_image_intent"] is False
    assert res["stripped_prompt"] == "hello world"


def test_parse_inline_query_image_intent():
    res = parse_inline_query("нарисуй кота")
    assert res["is_image_intent"] is True
    assert res["stripped_prompt"] == "кота"
    assert res["has_edit_intent"] is False
    assert res["has_quoted_text"] is False


def test_parse_inline_query_image_intent_quoted():
    res = parse_inline_query('нарисуй кота с надписью "привет"')
    assert res["is_image_intent"] is True
    assert res["has_quoted_text"] is True


def test_parse_inline_query_edit_intent():
    # Use an edit keyword, e.g. "измени"
    res = parse_inline_query("измени фото")
    assert res["is_image_intent"] is True
    assert res["has_edit_intent"] is True


@pytest.mark.asyncio
async def test_stream_inline_fast_suspends_key_related_error_chunks(monkeypatch):
    from app.handlers import inline

    quota_text = tag_error(ErrorCode.QUOTA_EXCEEDED, "quota exhausted")

    class FakeUseCase:
        def __init__(self) -> None:
            self.resolve_calls: list[dict] = []
            self.usages_incremented: list[tuple[str, str]] = []

        async def resolve_ai_request(self, preferred_model, excluded_key_hashes=None, **kwargs):
            excluded = set(excluded_key_hashes or set())
            self.resolve_calls.append({"preferred_model": preferred_model, "excluded": excluded})
            if len(self.resolve_calls) == 1:
                return {"api_key": "key-1", "key_hash": "hash1"}, preferred_model, None
            if len(self.resolve_calls) == 2:
                return {"api_key": "key-2", "key_hash": "hash2"}, preferred_model, None
            return None, None, "all_exhausted"

        async def increment_key_usage(self, key_hash, model_name, use_openrouter=False):
            self.usages_incremented.append((key_hash, model_name))

    class FakeStatusManager:
        def __init__(self) -> None:
            self.suspended: list[tuple[str, str, str]] = []
            self.successes: list[tuple[str, str]] = []

        async def suspend_key(self, key_hash, model_name, error_category, error_text=""):
            self.suspended.append((key_hash, model_name, error_category))

        async def record_success(self, key_hash, model_name):
            self.successes.append((key_hash, model_name))

    class FakeProvider:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        async def stream_response(self, **kwargs):
            if self.api_key == "key-1":
                yield quota_text
                return
            await asyncio.sleep(0.01)
            yield "fallback answer"

    fake_use_case = FakeUseCase()
    fake_status = FakeStatusManager()

    monkeypatch.setattr("app.agent_use_cases.AgentRequestUseCase", lambda: fake_use_case)
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: fake_status)
    monkeypatch.setattr("app.providers.base.get_provider_for_model", lambda _model, key: FakeProvider(key))
    monkeypatch.setattr("app.providers.gemini.get_vertex_client", lambda: None)
    monkeypatch.setattr(inline, "get_global_setting", _async_return("low"))

    text, sources = await inline._stream_inline_fast(
        preferred_model="gemini-3.1-flash-lite",
        history=[{"role": "user", "parts": ["hi"]}],
        system_instruction=None,
        user_id=123,
        max_rounds=1,
    )

    assert text == "fallback answer"
    assert sources == []
    assert fake_status.suspended == [("hash1", "gemini-3.1-flash-lite", "quota")]
    assert fake_status.successes == [("hash2", "gemini-3.1-flash-lite")]


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def test_select_inline_generation_model_uses_lite_for_simple_query():
    from app.handlers import inline

    assert (
        inline._select_inline_generation_model(
            configured_model="gemini-3.5-flash",
            user_query="когда вышел первый айфон?",
        )
        == "gemini-3.1-flash-lite"
    )


def test_should_use_inline_web_search_only_for_time_sensitive_queries():
    from app.handlers import inline

    assert inline._should_use_inline_web_search("когда вышел первый айфон?") is False
    assert inline._should_use_inline_web_search("объясни разницу между http и https") is False
    assert inline._should_use_inline_web_search("какой курс доллара сегодня?") is True
    assert inline._should_use_inline_web_search("погода в Киеве завтра") is True
    assert inline._should_use_inline_web_search("последние новости OpenAI") is True


def test_select_inline_generation_model_keeps_primary_for_contract_query():
    from app.handlers import inline

    assert (
        inline._select_inline_generation_model(
            configured_model="gemini-3.5-flash",
            user_query="составь договор на передачу моей собственной попы моему другу",
        )
        == "gemini-3.5-flash"
    )


@pytest.mark.asyncio
async def test_generate_inline_answer_returns_lite_when_primary_misses_deadline(monkeypatch):
    from app.handlers import inline

    router_calls: list[dict] = []
    lite_calls: list[dict] = []

    class FakeRouter:
        async def stream_response(self, **kwargs):
            router_calls.append(kwargs)
            await asyncio.sleep(0.2)
            yield "primary answer"

    async def fake_stream_inline_fast(**kwargs):
        lite_calls.append(kwargs)
        await asyncio.sleep(0.01)
        return "lite answer", []

    monkeypatch.setattr("app.providers.router.get_provider_router", lambda: FakeRouter())
    monkeypatch.setattr(inline, "_stream_inline_fast", fake_stream_inline_fast)
    monkeypatch.setattr(inline, "_INLINE_PRIMARY_GRACE_S", 0.05, raising=False)
    monkeypatch.setattr(inline, "get_global_setting", _async_return("low"))

    text, sources, model_used = await inline._generate_inline_answer(
        preferred_model="gemini-3.5-flash",
        user_query="составь договор на передачу моей собственной попы моему другу",
        history=[{"role": "user", "parts": ["draft a contract"]}],
        system_instruction="system",
        user_id=123,
        enable_web_search=False,
    )

    assert text == "lite answer"
    assert sources == []
    assert model_used == "gemini-3.1-flash-lite"
    assert router_calls[0]["preferred_model"] == "gemini-3.5-flash"
    assert lite_calls[0]["preferred_model"] == "gemini-3.1-flash-lite"


@pytest.mark.asyncio
async def test_generate_inline_answer_uses_grounding_standby_when_primary_misses_deadline(monkeypatch):
    from app.handlers import inline

    calls: list[dict] = []

    async def fake_stream_inline_fast(**kwargs):
        calls.append(kwargs)
        if kwargs["preferred_model"] == "gemini-2.5-flash":
            await asyncio.sleep(0.2)
            return "primary grounded answer", [("https://example.com/primary", "Primary")]
        await asyncio.sleep(0.01)
        return "standby grounded answer", [("https://example.com/standby", "Standby")]

    monkeypatch.setattr(inline, "_stream_inline_fast", fake_stream_inline_fast)
    monkeypatch.setattr(inline, "_INLINE_PRIMARY_GRACE_S", 0.05, raising=False)

    text, sources, model_used = await inline._generate_inline_answer(
        preferred_model="gemini-3.5-flash",
        user_query="какой курс доллара сегодня?",
        history=[{"role": "user", "parts": ["какой курс доллара сегодня?"]}],
        system_instruction="system",
        user_id=123,
        enable_web_search=True,
    )

    assert text == "standby grounded answer"
    assert sources == [("https://example.com/standby", "Standby")]
    assert model_used == "gemini-2.5-flash-lite"
    assert [call["preferred_model"] for call in calls] == ["gemini-2.5-flash", "gemini-2.5-flash-lite"]
    assert all(call["enable_web_search"] is True for call in calls)


@pytest.mark.asyncio
async def test_generate_inline_answer_skips_primary_for_simple_query(monkeypatch):
    from app.handlers import inline

    router_calls: list[dict] = []
    lite_calls: list[dict] = []

    class FakeRouter:
        async def stream_response(self, **kwargs):
            router_calls.append(kwargs)
            yield "primary answer"

    async def fake_stream_inline_fast(**kwargs):
        lite_calls.append(kwargs)
        return "lite answer", []

    monkeypatch.setattr("app.providers.router.get_provider_router", lambda: FakeRouter())
    monkeypatch.setattr(inline, "_stream_inline_fast", fake_stream_inline_fast)

    text, sources, model_used = await inline._generate_inline_answer(
        preferred_model="gemini-3.5-flash",
        user_query="когда вышел первый айфон?",
        history=[{"role": "user", "parts": ["когда вышел первый айфон?"]}],
        system_instruction="system",
        user_id=123,
        enable_web_search=False,
    )

    assert text == "lite answer"
    assert sources == []
    assert model_used == "gemini-3.1-flash-lite"
    assert router_calls == []
    assert lite_calls[0]["preferred_model"] == "gemini-3.1-flash-lite"
    assert lite_calls[0]["enable_web_search"] is False
