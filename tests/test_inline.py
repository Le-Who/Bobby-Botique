import asyncio
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_build_continue_keyboard_uses_context_token_for_inline_followup(monkeypatch):
    from app.handlers import inline

    stored_calls: list[dict] = []
    long_query = "поясни архитектуру очень подробно " * 8

    async def fake_store_inline_context(token, payload, user_id=None):
        stored_calls.append({"token": token, "payload": payload, "user_id": user_id})
        return True

    monkeypatch.setattr(inline.uuid, "uuid4", lambda: SimpleNamespace(hex="0123456789abcdef9999"))
    monkeypatch.setattr(inline, "store_inline_context", fake_store_inline_context)

    keyboard = await inline._build_continue_keyboard(
        bot_username="gemaibotv2",
        user_query=long_query,
        final_answer="предыдущий ответ",
        tone_id="friendly",
        lang="ru",
        user_id=42,
    )

    continue_button, ask_more_button = keyboard.inline_keyboard[0]

    assert continue_button.text == "💬 Обсудить в ЛС"
    assert continue_button.url == "https://t.me/gemaibotv2?start=ctx_0123456789abcdef"
    assert ask_more_button.text == "🔄 Ещё вопрос"
    assert ask_more_button.switch_inline_query_current_chat == "↪ 0123456789abcdef "
    assert ask_more_button.switch_inline_query_current_chat != long_query[:50]
    assert stored_calls == [
        {
            "token": "0123456789abcdef",
            "payload": {
                "q": long_query[:500],
                "a": "предыдущий ответ",
                "tone": "friendly",
            },
            "user_id": 42,
        }
    ]


def test_parse_inline_followup_query_extracts_token_and_new_question():
    from app.handlers import inline

    parsed = inline._parse_inline_followup_query("↪ 0123456789abcdef чем это отличается?")

    assert parsed == ("0123456789abcdef", "чем это отличается?")


def test_parse_inline_followup_query_accepts_telegram_emoji_arrow():
    from app.handlers import inline

    parsed = inline._parse_inline_followup_query("↪️ 3ca2f7941e084f8a разложи музыкально")

    assert parsed == ("3ca2f7941e084f8a", "разложи музыкально")


@pytest.mark.asyncio
async def test_chosen_inline_followup_submits_generation_with_context(monkeypatch):
    import app.utils.background_tasks as background_tasks
    from app.handlers import inline

    context_payload = {
        "q": "первый вопрос",
        "a": "первый ответ",
        "tone": "sarcastic",
    }
    captured: dict = {}

    async def fake_get_inline_context(token):
        assert token == "0123456789abcdef"
        return context_payload

    def fake_generate_and_edit_inline(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(close=lambda: None)

    class FakeTaskManager:
        def submit(self, task):
            captured["submitted_task"] = task

    chosen = SimpleNamespace(
        inline_message_id="inline-1",
        query="↪ 0123456789abcdef чем продолжим?",
        result_id="ctx_followup",
        from_user=SimpleNamespace(id=42, language_code="ru"),
    )
    update = SimpleNamespace(chosen_inline_result=chosen)
    context = SimpleNamespace(bot=SimpleNamespace(first_name="GemAI"))

    monkeypatch.setattr(inline, "get_inline_context", fake_get_inline_context)
    monkeypatch.setattr(inline, "_generate_and_edit_inline", fake_generate_and_edit_inline)
    monkeypatch.setattr(background_tasks, "get_task_manager", lambda: FakeTaskManager())

    await inline.handle_chosen_inline_result(update, context)

    assert captured["inline_message_id"] == "inline-1"
    assert captured["user_query"] == "чем продолжим?"
    assert captured["tone_id"] == "sarcastic"
    assert captured["user_id"] == 42
    assert captured["inline_context"] == context_payload


@pytest.mark.asyncio
async def test_chosen_inline_followup_without_question_edits_hint(monkeypatch):
    from app.handlers import inline

    async def fake_get_inline_context(token):
        assert token == "0123456789abcdef"
        return {
            "q": "первый вопрос",
            "a": "первый ответ",
            "tone": "friendly",
        }

    class FakeBot:
        async def edit_message_text(self, **kwargs):
            captured["edit_kwargs"] = kwargs

    captured: dict = {}
    chosen = SimpleNamespace(
        inline_message_id="inline-1",
        query="↪ 0123456789abcdef",
        result_id="ctx_hint",
        from_user=SimpleNamespace(id=42, language_code="ru"),
    )
    update = SimpleNamespace(chosen_inline_result=chosen)
    context = SimpleNamespace(bot=FakeBot())

    monkeypatch.setattr(inline, "get_inline_context", fake_get_inline_context)

    await inline.handle_chosen_inline_result(update, context)

    assert captured["edit_kwargs"]["text"] == "Допишите новый вопрос после стрелки"


@pytest.mark.asyncio
async def test_generate_and_edit_inline_includes_inline_context_in_history(monkeypatch):
    from app.handlers import inline

    captured: dict = {}

    async def fake_generate_inline_answer(**kwargs):
        captured["history"] = kwargs["history"]
        return "новый ответ", [], "gemini-3.1-flash-lite"

    class FakeMetrics:
        async def record_api_call(self, *args, **kwargs):
            return None

        async def record_request(self, *args, **kwargs):
            return None

    class FakeLogger:
        def log_request(self, *args, **kwargs):
            return "request-start"

        def log_response(self, *args, **kwargs):
            captured["logged_response"] = kwargs

    class FakeBot:
        first_name = "GemAI"
        username = "gemaibotv2"

        async def edit_message_text(self, **kwargs):
            captured["edit_kwargs"] = kwargs

    monkeypatch.setattr(inline, "get_global_setting", _async_return("off"))
    monkeypatch.setattr(inline, "get_inline_model", _async_return("gemini-3.1-flash-lite"))
    monkeypatch.setattr(inline, "_generate_inline_answer", fake_generate_inline_answer)
    monkeypatch.setattr(inline, "metrics_collector", FakeMetrics())
    monkeypatch.setattr(inline, "api_logger", FakeLogger())
    monkeypatch.setattr(inline, "store_inline_context", _async_return(True))
    monkeypatch.setattr(inline.uuid, "uuid4", lambda: SimpleNamespace(hex="fedcba98765432100000"))

    await inline._generate_and_edit_inline(
        bot=FakeBot(),
        inline_message_id="inline-2",
        user_query="а какие риски?",
        tone_id="friendly",
        user_id=42,
        lang="ru",
        inline_context={
            "q": "как спроектировать систему?",
            "a": "предыдущий ответ",
            "tone": "friendly",
        },
    )

    assert captured["history"] == [
        {"role": "user", "parts": ["как спроектировать систему?"]},
        {"role": "model", "parts": ["предыдущий ответ"]},
        {"role": "user", "parts": ["а какие риски?"]},
    ]
    assert "новый ответ" in captured["edit_kwargs"]["text"]


def test_select_inline_generation_model_uses_lite_for_simple_query():
    from app.handlers import inline

    assert (
        inline._select_inline_generation_model(
            configured_model="gemini-3.5-flash",
            user_query="когда вышел первый айфон?",
        )
        == "gemini-3.1-flash-lite"
    )


def test_select_inline_generation_model_uses_lite_for_short_creative_query():
    from app.handlers import inline

    assert (
        inline._select_inline_generation_model(
            configured_model="gemini-3.5-flash",
            user_query="сочини-ка мне лютую рэпчинку на свободную темку, с плавным флоу",
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
