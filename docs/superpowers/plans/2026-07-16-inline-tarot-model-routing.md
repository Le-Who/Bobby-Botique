# Inline Tarot Model Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route simple inline Tarot spreads exclusively through `gemini-3.1-flash-lite`, while giving `tarot_love` and `tarot_celtic` at most 23 seconds to return a stronger `gemini-3.5-flash` answer before publishing the ready lite answer.

**Architecture:** Keep Tarot prompt construction and Telegram editing in `_generate_tarot_inline()`. Introduce explicit Tarot model-policy constants and a focused `_generate_tarot_response()` coordinator that owns the two-task race, validates provider responses, and always cancels pending work. Continue using `ProviderRouter.get_response()` so key rotation and retry behavior remain unchanged.

**Tech Stack:** Python 3.14, `asyncio`, pytest, `pytest-asyncio`, `unittest.mock`, python-telegram-bot.

---

## File Map

- Modify `app/handlers/inline.py`: define the Tarot model policy, coordinate complex-spread tasks, and call the coordinator from `_generate_tarot_inline()`.
- Modify `tests/test_tarot_inline_retry.py`: replace the obsolete two-model assertion for classic Tarot and add deterministic timing, selection, failure, and cancellation coverage.
- Verify `tests/test_tarot.py`, `tests/test_tarot_chat.py`, and `tests/test_inline.py`: ensure prompt behavior, non-inline Tarot, and ordinary inline routing remain unchanged.

The worktree already contains unrelated user changes in `app/handlers/inline.py` and `tests/test_inline.py`. Preserve them exactly. When committing, stage only the Tarot hunks from `app/handlers/inline.py`; never stage or rewrite the follow-up marker hunk near `_INLINE_FOLLOWUP_RE`.

### Task 1: Make Simple Inline Tarot Spreads Lite-Only

**Files:**
- Modify: `tests/test_tarot_inline_retry.py:26-63`
- Modify: `app/handlers/inline.py:79-90`
- Modify: `app/handlers/inline.py:2609-2645`

- [ ] **Step 1: Replace the obsolete two-model classic test with a failing policy test**

Replace `test_generate_tarot_inline_uses_provider_router` with:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("spread_type", ["tarot", "tarot_daily", "tarot_yesno"])
async def test_generate_tarot_inline_uses_only_flash_lite_for_simple_spreads(spread_type):
    """One- and three-card inline spreads must not consume a 3.5 Flash request."""
    from app.handlers.inline import _generate_tarot_inline

    bot = _make_bot()
    mock_router = AsyncMock()
    mock_router.get_response.return_value = ("Карты говорят: всё будет хорошо.", 100)

    with (
        patch("app.providers.router.get_provider_router", return_value=mock_router),
        patch("app.tarot_daily.get_prepared_daily_reading", new=AsyncMock(return_value=None)),
        patch("app.tarot_daily.upsert_prepared_daily_reading", new=AsyncMock()),
    ):
        await _generate_tarot_inline(
            bot=bot,
            inline_message_id="test-simple-spread",
            user_query="таро будем ли мы спорить?",
            user_id=123,
            spread_type=spread_type,
        )

    mock_router.get_response.assert_awaited_once()
    call = mock_router.get_response.await_args.kwargs
    assert call["preferred_model"] == "gemini-3.1-flash-lite"
    assert call["max_key_retries"] == 3
    assert call["use_openrouter"] is False
    assert "❌" not in bot.edit_message_text.await_args.kwargs["text"]
```

- [ ] **Step 2: Run the policy test and verify RED**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py::test_generate_tarot_inline_uses_only_flash_lite_for_simple_spreads
```

Expected: `tarot` and `tarot_yesno` fail because the current function makes two calls and selects `gemini-3.5-flash`; `tarot_daily` already passes.

- [ ] **Step 3: Add explicit Tarot model-policy constants**

Add after `_INLINE_PRIMARY_GRACE_S` in `app/handlers/inline.py`:

```python
_TAROT_LITE_MODEL = "gemini-3.1-flash-lite"
_TAROT_PRIMARY_MODEL = "gemini-3.5-flash"
_TAROT_COMPLEX_SPREADS = frozenset({SpreadType.LOVE, SpreadType.CELTIC})
_TAROT_COMPLEX_PRIMARY_GRACE_S = 23.0
```

- [ ] **Step 4: Restrict the existing hot standby path to complex spreads**

In `_generate_tarot_inline()`, replace the model selection and standby creation with:

```python
    is_complex_spread = spread in _TAROT_COMPLEX_SPREADS
    preferred_model = _TAROT_PRIMARY_MODEL if is_complex_spread else _TAROT_LITE_MODEL

    _lite_task: asyncio.Task | None = None
    if is_complex_spread:
        _lite_task = asyncio.create_task(
            router.get_response(
                preferred_model=_TAROT_LITE_MODEL,
                history=_history,
                system_instruction=system_instruction,
                user_id=user_id,
                use_openrouter=False,
                max_key_retries=3,
            )
        )
```

Leave the remaining fallback block unchanged for this TDD cycle. It will be replaced by the bounded coordinator in Task 2.

- [ ] **Step 5: Run the policy test and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py::test_generate_tarot_inline_uses_only_flash_lite_for_simple_spreads
```

Expected: `3 passed`.

- [ ] **Step 6: Run the existing Tarot inline regression file**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py
```

Expected: all tests pass. The total-failure and provider-exception tests must still edit the inline placeholder with the retry UI.

- [ ] **Step 7: Commit only the Task 1 hunks**

Stage `tests/test_tarot_inline_retry.py` normally, but interactively stage only the Tarot constants/model-selection hunks from `app/handlers/inline.py`:

```powershell
git add -- tests/test_tarot_inline_retry.py
git add -p -- app/handlers/inline.py
git diff --cached --check
git diff --cached --name-only
git commit -m "Route simple inline tarot through flash lite"
```

Expected staged files: `tests/test_tarot_inline_retry.py` and `app/handlers/inline.py`. Reject the pre-existing `_INLINE_FOLLOWUP_RE` hunk when `git add -p` asks about it.

### Task 2: Bound Complex Tarot Primary Selection to 23 Seconds

**Files:**
- Modify: `tests/test_tarot_inline_retry.py`
- Modify: `app/handlers/inline.py:2509-2685`

- [ ] **Step 1: Add imports for deterministic asynchronous timing tests**

Add at the top of `tests/test_tarot_inline_retry.py`:

```python
import asyncio
import time
```

- [ ] **Step 2: Add failing tests for strong-model selection and cleanup**

Append:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("spread_name", ["LOVE", "CELTIC"])
async def test_generate_tarot_response_prefers_complex_primary_within_deadline(spread_name):
    from app.handlers import inline
    from app.tarot import SpreadType

    calls: list[str] = []
    lite_cancelled = asyncio.Event()

    async def get_response(**kwargs):
        model = kwargs["preferred_model"]
        calls.append(model)
        if model == "gemini-3.5-flash":
            await asyncio.sleep(0.01)
            return "strong answer", 100
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            lite_cancelled.set()
            raise
        return "lite answer", 50

    router = MagicMock()
    router.get_response = AsyncMock(side_effect=get_response)

    result = await inline._generate_tarot_response(
        router=router,
        spread=getattr(SpreadType, spread_name),
        history=[{"role": "user", "parts": ["question"]}],
        system_instruction="system",
        user_id=123,
    )

    assert result == "strong answer"
    assert set(calls) == {"gemini-3.5-flash", "gemini-3.1-flash-lite"}
    assert lite_cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("spread_name", ["LOVE", "CELTIC"])
async def test_generate_tarot_response_uses_ready_lite_after_primary_deadline(monkeypatch, spread_name):
    from app.handlers import inline
    from app.tarot import SpreadType

    calls: list[str] = []
    primary_cancelled = asyncio.Event()

    async def get_response(**kwargs):
        model = kwargs["preferred_model"]
        calls.append(model)
        if model == "gemini-3.5-flash":
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                primary_cancelled.set()
                raise
            return "late strong answer", 100
        await asyncio.sleep(0.01)
        return "ready lite answer", 50

    router = MagicMock()
    router.get_response = AsyncMock(side_effect=get_response)
    monkeypatch.setattr(inline, "_TAROT_COMPLEX_PRIMARY_GRACE_S", 0.05)

    started = time.monotonic()
    result = await inline._generate_tarot_response(
        router=router,
        spread=getattr(SpreadType, spread_name),
        history=[{"role": "user", "parts": ["question"]}],
        system_instruction="system",
        user_id=123,
    )
    elapsed = time.monotonic() - started

    assert result == "ready lite answer"
    assert elapsed < 0.25
    assert set(calls) == {"gemini-3.5-flash", "gemini-3.1-flash-lite"}
    assert primary_cancelled.is_set()


@pytest.mark.asyncio
async def test_generate_tarot_response_uses_lite_immediately_when_primary_fails():
    from app.handlers import inline
    from app.tarot import SpreadType

    async def get_response(**kwargs):
        if kwargs["preferred_model"] == "gemini-3.5-flash":
            raise RuntimeError("primary unavailable")
        return "lite after failure", 50

    router = MagicMock()
    router.get_response = AsyncMock(side_effect=get_response)

    result = await inline._generate_tarot_response(
        router=router,
        spread=SpreadType.LOVE,
        history=[{"role": "user", "parts": ["question"]}],
        system_instruction="system",
        user_id=123,
    )

    assert result == "lite after failure"
```

- [ ] **Step 3: Run the new coordinator tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py -k "generate_tarot_response"
```

Expected: failures with `AttributeError` because `_generate_tarot_response` does not exist yet.

- [ ] **Step 4: Implement the bounded response coordinator**

Add immediately before `_generate_tarot_inline()`:

```python
async def _generate_tarot_response(
    router,
    spread: SpreadType,
    history: list,
    system_instruction: str,
    user_id: int | None,
) -> str | None:
    """Return a Tarot answer according to the inline spread model policy."""

    async def _call(model: str) -> str | None:
        response, _tokens = await router.get_response(
            preferred_model=model,
            history=history,
            system_instruction=system_instruction,
            user_id=user_id,
            use_openrouter=False,
            max_key_retries=3,
        )
        return response

    if spread not in _TAROT_COMPLEX_SPREADS:
        return await _call(_TAROT_LITE_MODEL)

    primary_task = asyncio.create_task(_call(_TAROT_PRIMARY_MODEL))
    lite_task = asyncio.create_task(_call(_TAROT_LITE_MODEL))
    primary_result: str | None = None
    lite_result: str | None = None

    try:
        try:
            primary_result = await asyncio.wait_for(
                asyncio.shield(primary_task),
                timeout=_TAROT_COMPLEX_PRIMARY_GRACE_S,
            )
        except TimeoutError:
            logging.info(
                "Inline Tarot primary %s missed %.0fs deadline for spread=%s",
                _TAROT_PRIMARY_MODEL,
                _TAROT_COMPLEX_PRIMARY_GRACE_S,
                spread.value,
            )
        except Exception as exc:
            logging.warning(
                "Inline Tarot primary failed for spread=%s: %s",
                spread.value,
                exc,
            )

        if _is_usable_inline_answer(primary_result):
            return primary_result

        try:
            lite_result = await lite_task
        except Exception as exc:
            logging.warning(
                "Inline Tarot lite fallback failed for spread=%s: %s",
                spread.value,
                exc,
            )

        if _is_usable_inline_answer(lite_result):
            return lite_result

        if primary_result is None and primary_task.done():
            try:
                primary_result = primary_task.result()
            except Exception as exc:
                logging.warning(
                    "Inline Tarot primary finished with an error for spread=%s: %s",
                    spread.value,
                    exc,
                )

        if _is_usable_inline_answer(primary_result):
            return primary_result
        return lite_result if lite_result is not None else primary_result
    finally:
        for task in (primary_task, lite_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
```

- [ ] **Step 5: Replace the bespoke Tarot hot-standby block with the coordinator**

In `_generate_tarot_inline()`, keep `router` and `_history`, then replace the generation/fallback block with:

```python
    try:
        result = await _generate_tarot_response(
            router=router,
            spread=spread,
            history=_history,
            system_instruction=system_instruction,
            user_id=user_id,
        )
    except Exception as exc:
        logging.error(
            "Tarot generation exception (spread=%s): %s",
            spread_type,
            exc,
            exc_info=True,
        )
        await _edit_failure("ошибка генерации. Попробуйте ещё раз.")
        return
```

Delete `_lite_task`, `_primary_failed_exc`, `_need_lite`, `_used_fallback`, and their old cancellation/fallback branches. Keep the existing `if not result ...` validation directly after the new block.

- [ ] **Step 6: Run the coordinator tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py -k "generate_tarot_response"
```

Expected: `5 passed` (two parameterized primary-winner cases, two timeout cases, and one early-failure case).

- [ ] **Step 7: Run all Tarot inline regression tests**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py
```

Expected: all tests pass, including the existing total-failure, tagged-error, and retry-button assertions.

- [ ] **Step 8: Commit only the Task 2 hunks**

```powershell
git add -- tests/test_tarot_inline_retry.py
git add -p -- app/handlers/inline.py
git diff --cached --check
git diff --cached --name-only
git commit -m "Bound complex inline tarot model wait"
```

Again reject any unrelated pre-existing hunk from `app/handlers/inline.py`.

### Task 3: Verify Scope, Encoding, and Regressions

**Files:**
- Verify: `app/handlers/inline.py`
- Verify: `app/handlers/tarot_chat.py`
- Verify: `tests/test_tarot_inline_retry.py`
- Verify: `tests/test_tarot.py`
- Verify: `tests/test_tarot_chat.py`
- Verify: `tests/test_inline.py`

- [ ] **Step 1: Run the focused Tarot and inline suites**

Run:

```powershell
python -m pytest -q tests/test_tarot_inline_retry.py tests/test_tarot.py tests/test_tarot_chat.py tests/test_inline.py
```

Expected: all selected tests pass. In particular, non-inline tests in `tests/test_tarot_chat.py` prove the chat-session route remains unchanged.

- [ ] **Step 2: Run static checks on the changed Python files**

Run:

```powershell
python -m ruff check app/handlers/inline.py tests/test_tarot_inline_retry.py
python -m compileall -q app/handlers/inline.py tests/test_tarot_inline_retry.py
```

Expected: both commands exit with status 0 and produce no diagnostics.

- [ ] **Step 3: Verify repository encoding before and after final diff review**

Run:

```powershell
python scripts/check_encoding.py
git diff --check
```

Expected: both commands exit with status 0. Do not alter emoji or documentation based on terminal rendering.

- [ ] **Step 4: Audit the final behavioral matrix in source and tests**

Run:

```powershell
rg -n "_TAROT_(LITE_MODEL|PRIMARY_MODEL|COMPLEX_SPREADS|COMPLEX_PRIMARY_GRACE_S)|_generate_tarot_response" app/handlers/inline.py tests/test_tarot_inline_retry.py
git status --short
git diff -- app/handlers/inline.py tests/test_tarot_inline_retry.py tests/test_inline.py
```

Expected evidence:

- `_TAROT_COMPLEX_PRIMARY_GRACE_S = 23.0`.
- Only `SpreadType.LOVE` and `SpreadType.CELTIC` are in `_TAROT_COMPLEX_SPREADS`.
- Classic, daily, and yes/no tests assert one lite call.
- Complex timeout tests assert the ready lite answer and cancellation of the primary.
- The unrelated changes in `app/handlers/inline.py` and `tests/test_inline.py` remain present and unmodified.

- [ ] **Step 5: Report verification without staging unrelated work**

Summarize the model matrix, the root cause, test counts, and any residual risk. Do not stage or commit `tests/test_inline.py`; it belongs to the pre-existing follow-up-query work.
