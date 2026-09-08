from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.games import crocodile_daily as daily
from app.repos import crocodile_daily as repo


@pytest.mark.asyncio
async def test_daily_text_setting_defaults_to_automatic(monkeypatch):
    from app.games.daily_ai import get_daily_text_model

    async def setting(key, default):
        assert key == "daily_croc_text_model"
        assert default == ""
        return default

    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", setting)
    assert await get_daily_text_model() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-3.1-flash-lite", "gemini-2.5-flash"])
async def test_daily_text_uses_selected_model_via_genai_sdk(monkeypatch, model):
    from app.games.daily_ai import generate_daily_text

    async def select_key(model_name):
        assert model_name == model
        return {"api_key": "test-key", "key_hash": "test-hash"}

    async def reserve(key_hash, model_name):
        assert key_hash == "test-hash"
        assert model_name == model
        return True

    async def generate(**kwargs):
        assert kwargs["model"] == model
        assert kwargs["contents"] == "prompt"
        assert kwargs["config"].response_mime_type == "application/json"
        return SimpleNamespace(text="answer")

    def client(api_key):
        assert api_key == "test-key"
        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", select_key)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", reserve)
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", client)
    monkeypatch.setattr("app.providers.router.get_provider_router", lambda: pytest.fail("must not route"))
    assert await generate_daily_text("prompt", model) == "answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["pollinations/openai", "opencode-go/minimax-m2.5", "gpt-5"])
async def test_daily_text_rejects_non_gemini_models_before_provider_access(monkeypatch, model):
    from app.games.daily_ai import generate_daily_text

    monkeypatch.setattr("app.providers.router.get_provider_router", lambda: pytest.fail("must not route"))
    with pytest.raises(ValueError, match="Gemini"):
        await generate_daily_text("prompt", model)


@pytest.mark.asyncio
async def test_daily_text_does_not_call_sdk_without_available_quota(monkeypatch):
    from app.games.daily_ai import generate_daily_text

    monkeypatch.setattr(
        "app.repos.keys.get_available_gemini_key", AsyncMock(return_value={"api_key": "key", "key_hash": "hash"})
    )
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=False))
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", lambda key: pytest.fail("quota exhausted"))
    with pytest.raises(RuntimeError, match="quota"):
        await generate_daily_text("prompt", "gemini-3.1-flash-lite")


@pytest.mark.asyncio
async def test_shared_text_retires_failed_key_before_next_request(monkeypatch):
    from app.games.daily_ai import generate_daily_text

    state = {"failed": False}
    model = "gemini-2.5-flash"

    async def select_key(model_name):
        return {"api_key": "healthy" if state["failed"] else "bad-secret", "key_hash": "hash"}

    async def suspend(key_hash, model_name, category, error_text):
        assert (key_hash, model_name, category) == ("hash", model, "permanent")
        assert "bad-secret" not in error_text
        state["failed"] = True

    def client(key):
        async def generate(**kwargs):
            if key == "bad-secret":
                raise RuntimeError("invalid api key: bad-secret")
            return SimpleNamespace(text='{"ok":true}')

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))

    monkeypatch.setattr("app.repos.keys.get_available_gemini_key", select_key)
    monkeypatch.setattr("app.repos.keys.reserve_gemini_key_usage", AsyncMock(return_value=True))
    monkeypatch.setattr("app.repos.keys.get_key_status_manager", lambda: SimpleNamespace(suspend_key=suspend))
    monkeypatch.setattr("app.providers.gemini.get_cached_genai_client", client)
    with pytest.raises(RuntimeError, match="invalid api key"):
        await generate_daily_text("prompt", model)
    assert await generate_daily_text("prompt", model) == '{"ok":true}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, expected",
    [("pollinations/openai", ""), ("opencode-go/minimax-m2.5", ""), ("gemini-2.5-flash", "gemini-2.5-flash")],
)
async def test_daily_text_setting_ignores_old_non_gemini_selection(monkeypatch, value, expected):
    from app.games.daily_ai import get_daily_text_model

    monkeypatch.setattr("app.repos.settings_repo.get_global_setting", AsyncMock(return_value=value))
    assert await get_daily_text_model() == expected


@pytest.mark.asyncio
async def test_daily_translation_uses_selected_model_and_model_scoped_cache(monkeypatch):
    from app.games import daily_ai, word_bank

    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.1-flash-lite"))
    monkeypatch.setattr(word_bank, "_PROMPT_TRANSLATION_CACHE", {"чайник": "old cached description"})

    async def generate(prompt, model, **kwargs):
        assert model == "gemini-3.1-flash-lite"
        return '{"visual_description":"a ceramic teapot", "is_drawable":true}'

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await daily._translate_word_for_prompt("чайник") == "a ceramic teapot"


@pytest.mark.asyncio
async def test_daily_selected_hints_are_saved_without_using_classic_generator(monkeypatch):
    from app.games import daily_ai

    puzzle = repo.DailyPuzzle(date(2026, 9, 8), "чайник", "предметы", "ru")
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.1-flash-lite"))
    monkeypatch.setattr(
        daily_ai,
        "generate_daily_text",
        AsyncMock(return_value='{"hints":["Есть ручка", "Бывает носик", "В нём кипятят воду"]}'),
    )
    saved = []

    async def save(day, hints, **kwargs):
        saved.extend(hints)

    monkeypatch.setattr(repo, "set_puzzle_hints", save)
    monkeypatch.setattr(
        "app.games.hinting.get_or_generate_cached_hints", AsyncMock(side_effect=AssertionError("classic"))
    )
    assert await daily.get_daily_hints(puzzle) == ["Есть ручка", "Бывает носик", "В нём кипятят воду"]
    assert saved == ["Есть ручка", "Бывает носик", "В нём кипятят воду"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"word":"самовар"}', '{"word":"  САМОвар  "}'])
async def test_selected_daily_word_is_normalized_and_excluded_words_are_in_prompt(monkeypatch, response):
    from app.games import daily_ai

    async def generate(prompt, model, **kwargs):
        assert "чайник" in prompt
        assert "hard" in prompt
        assert model == "gemini-3.1-flash-lite"
        return response

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    assert await daily_ai.generate_daily_word("предметы", "hard", {"чайник"}, "gemini-3.1-flash-lite") == "самовар"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"word":"Чайник"}', '{"word":"<script>"}', '{"word":123}', "[]", "not json"])
async def test_invalid_or_repeated_daily_words_are_rejected(monkeypatch, response):
    from app.games import daily_ai

    monkeypatch.setattr(daily_ai, "generate_daily_text", AsyncMock(return_value=response))
    assert await daily_ai.generate_daily_word("предметы", "easy", {"чайник"}, "gemini-3.1-flash-lite") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "response", "expected"),
    [
        ("gemini-3.1-flash-lite", '{"word":"самовар"}', "самовар"),
        ("", "", "лампа"),
        ("gemini-3.1-flash-lite", '{"word":"чайник"}', "лампа"),
    ],
)
async def test_create_daily_puzzle_uses_configured_model_with_wordbank_fallback(monkeypatch, model, response, expected):
    from app.games import daily_ai

    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value=model))
    monkeypatch.setattr(daily_ai, "generate_daily_text", AsyncMock(return_value=response))
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "ensure_puzzle_day", AsyncMock())
    monkeypatch.setattr(repo, "get_used_daily_words", AsyncMock(return_value={"чайник"}))
    monkeypatch.setattr(
        "app.games.word_bank.pick_random_word_for_topic", AsyncMock(return_value=("лампа", "ru", "Предметы", False))
    )

    async def query(sql, params, **kwargs):
        return [
            {
                "puzzle_date": params[0],
                "difficulty": params[1],
                "target_word": params[2],
                "topic": params[3],
                "lang": params[4],
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", query)
    monkeypatch.setattr(repo.db.db_manager, "pool", None)
    puzzle = await repo.create_puzzle_if_missing(date(2026, 9, 8))
    assert puzzle.target_word == expected
    assert puzzle.lang == "ru"


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [None, "gemini-2.5-flash"])
async def test_regenerate_daily_word_uses_selected_model_and_excludes_previous_word(monkeypatch, override):
    from app.games import daily_ai

    existing = repo.DailyPuzzle(date(2026, 9, 8), "чайник", "Предметы", "ru")
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=existing))
    monkeypatch.setattr(repo, "get_used_daily_words", AsyncMock(return_value=set()))
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.1-flash-lite"))

    async def generate(prompt, model, **kwargs):
        assert "чайник" in prompt
        assert model == (override or "gemini-3.1-flash-lite")
        return '{"word":"самовар"}'

    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)
    monkeypatch.setattr(
        "app.games.word_bank.pick_random_word_for_topic", AsyncMock(return_value=("лампа", "ru", "Предметы", False))
    )
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock(_closed=False)
    pool.acquire.return_value.__aenter__.return_value = conn
    monkeypatch.setattr(repo.db.db_manager, "pool", pool)

    async def query(sql, params, **kwargs):
        return [
            {
                "puzzle_date": params[3],
                "difficulty": params[4],
                "target_word": params[0],
                "topic": params[1],
                "lang": params[2],
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", query)
    puzzle = await repo.regenerate_puzzle_word(existing.puzzle_date, model=override)
    assert puzzle.target_word == "самовар"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "response", "expected"),
    [
        ("gemini-3.1-flash-lite", '{"hint":"Ищи среди кухонных предметов"}', "Обычная подсказка"),
        ("gemini-3.1-flash-lite", '{"hint":"Это чайник"}', "Обычная подсказка"),
        ("", "", "Обычная подсказка"),
    ],
)
async def test_daily_guess_uses_shared_judgement_without_second_generation(monkeypatch, model, response, expected):
    from app.games import daily_ai

    puzzle = repo.DailyPuzzle(date(2026, 9, 8), "чайник", "Предметы", "ru")
    result = SimpleNamespace(status="active", attempts=[], best_score=0.3)
    judgement = SimpleNamespace(score=0.3, hint="Обычная подсказка", cached=False)
    monkeypatch.setattr(daily, "get_daily_state", AsyncMock(return_value=(puzzle, result)))
    monkeypatch.setattr("app.games.judge.judge_guess", AsyncMock(return_value=("warm", judgement)))
    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value=model))

    generate = AsyncMock(return_value=response)
    monkeypatch.setattr(daily_ai, "generate_daily_text", generate)

    async def append(**kwargs):
        result.attempts.append(kwargs["attempt"])
        return result

    monkeypatch.setattr(repo, "append_attempt_and_maybe_finish", append)
    event = await daily.process_daily_guess(42, "тарелка")
    assert event["hint"].endswith(expected)
    assert event["score"] == 0.3
    assert event["status"] == "warm"
    assert result.attempts[0]["hint"] == event["hint"]
    assert judgement.hint == "Обычная подсказка"
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected, expected", [("pollinations", "qwen-image"), ("fta-gpt-image-2", "fta-gpt-image-2")])
async def test_new_daily_puzzle_records_global_image_model(monkeypatch, selected, expected):
    from app.games import daily_ai

    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value=""))
    monkeypatch.setattr(daily, "get_daily_image_model", AsyncMock(return_value=selected))
    monkeypatch.setattr(repo, "get_puzzle", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "ensure_puzzle_day", AsyncMock())
    monkeypatch.setattr(repo, "get_used_daily_words", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        "app.games.word_bank.pick_random_word_for_topic", AsyncMock(return_value=("лампа", "ru", "Предметы", False))
    )

    async def query(sql, params, **kwargs):
        return [
            {
                "puzzle_date": params[0],
                "difficulty": params[1],
                "target_word": params[2],
                "topic": params[3],
                "lang": params[4],
                "image_model": params[5],
            }
        ]

    monkeypatch.setattr(repo.db, "db_query", query)
    puzzle = await repo._create_puzzle_if_missing_with_conn(date(2026, 9, 8))
    assert puzzle.image_model == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["word", "hints"])
async def test_daily_generation_error_logs_do_not_expose_provider_credentials(monkeypatch, caplog, operation):
    from app.games import daily_ai

    monkeypatch.setattr(
        daily_ai, "generate_daily_text", AsyncMock(side_effect=ValueError("secret-provider-credential"))
    )
    if operation == "word":
        await daily_ai.generate_daily_word("Предметы", "easy", set(), "gemini-3.1-flash-lite")
    elif operation == "hints":
        await daily_ai.generate_daily_hints("чайник", "Предметы", "gemini-3.1-flash-lite")
    assert "secret-provider-credential" not in caplog.text
    assert "ValueError" in caplog.text


@pytest.mark.asyncio
async def test_selected_model_generates_visual_description_even_for_builtin_word(monkeypatch):
    from app.games import crocodile_daily as daily
    from app.games import daily_ai, word_bank

    monkeypatch.setattr(daily_ai, "get_daily_text_model", AsyncMock(return_value="gemini-3.1-flash-lite"))
    monkeypatch.setattr(word_bank, "get_english_equivalent", lambda word: "cat")
    monkeypatch.setattr(daily, "_translate_word_for_prompt", AsyncMock(return_value="a playful kitten in a basket"))
    prompt = await daily._build_daily_image_prompt("кот", "животные", difficulty="easy")
    assert '"a playful kitten in a basket"' in prompt
