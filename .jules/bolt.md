## 2026-04-18 - Inline regex patterns in hot-path intent detectors and formatters (cmd_image.py, msg_voice.py, text_format.py)

**Learning:** `check_draw_intent_async` in `cmd_image.py` compiled `_VERB_HEURISTIC`, `_should_auto_route` in `msg_voice.py` compiled `action_pattern`, and `sanitize_html_tags` in `text_format.py` compiled `_TAG_RE` inline. All of these functions execute extremely frequently (per intent check, per voice message, or per formatted output block). Inline `re.compile` forces the `re._cache` lookup, bypassing true zero-overhead evaluation and risking cache eviction under high concurrency.
**Action:** Relocated all static regex patterns (`_VERB_HEURISTIC`, `_VOICE_ACTION_PATTERN`, `_TAG_RE`, `_EMPTY_TAG_RE`) to module level constants to guarantee maximum performance per message. Pattern established: any `re.compile()` within an event handler or utility function triggered per-message must be ruthlessly extracted to module scope.

## 2026-04-18 - Constant Allocation Inside Hot Pre-Filter (intent_router.py)

**Learning:** `_handle_crypto()` rebuilt a 4-entry `_COIN_NAMES` dict on every call. `_extract_currency_pair()` called `sorted(_CURRENCY_CODES.keys(), key=len, reverse=True)` on every fiat query — both produce the same result on every invocation and ran on the path that fires for every user message matching the currency intent.
**Action:** Any `dict` literal, `sorted()`, or other collection construction that produces the same result every time belongs at module level. Always scan hot-path functions (called per user request) for local constant definitions that are candidates for module-level hoisting.

## 2026-04-18 - re.compile() Inside Overflow Handler Function (streaming.py)

**Learning:** `_detect_open_markdown()` called `re.compile(r"^```", re.MULTILINE)` plus three anonymous `re.sub()`/`re.match()` calls — totalling 4 regex compilations — on every invocation. This function fires on every streaming message split (when a response overflows 4000 chars), not once per session. The `re` module caches compiled patterns internally (`re._cache`, 512 slots) but the cache lookup itself has overhead and can evict under load. More importantly, `re.compile()` inside a function body is a clear code smell that signals unintentional repetition.
**Action:** Any regex that is not parameterised (pattern string is a literal) belongs at module level as a compiled constant (`_MD_FENCE_RE`, etc.). The pattern: look for `re.compile(...)` or `re.sub(r"..."` literals inside any function that is called more than once — especially in event-driven or per-message async handlers.

## 2026-04-18 - Two-Pass Text Scan in detect_language() (i18n.py)

**Learning:** `detect_language()` iterated `text` twice: once via `_CYRILLIC_RE.findall(text)` (allocating a list of N single-char strings) and again via `sum(1 for ch in text if ch.isalpha())`. For a 500-char message this creates ~500 temporary string allocations + one list. This function fires on every single user message across three hot handlers: `messages.py`, `ai_chat.py`, `msg_voice.py` — making it one of the highest-frequency functions in the entire application.
**Action:** Replaced the two-pass approach with a single `for ch in text` loop that counts both `cyrillic_count` and `alpha_count` simultaneously. The Cyrillic check (`"\u0400" <= ch <= "\u04ff"`) is a pure Unicode range comparison — faster than regex and avoids all intermediate allocations. `_CYRILLIC_RE` is now unused and was removed. Verified: 80/80 tests pass.

## 2026-04-18 - 18 Inline Regex Patterns in Reader Render Pipeline (reader_utils.py)

**Learning:** `reader_utils.py` had ~18 inline regex operations scattered across hot render functions: 2× `re.compile()` inside `apply_bionic_reading()` (compiled fresh on every call w/ HTML content), 6× `re.sub(r"...", ...)` raw patterns in `_inline_markup()` (called per paragraph/list item), and 9× inline `re.match()`/`re.sub()` in the block-level render loop of `markdown_to_reader_html()` (called per line of output). The `_slug()` and `extract_toc()` functions also had inline `re.sub()` calls.
**Action:** Hoisted all 18 patterns to module-level pre-compiled constants (`_SLUG_STRIP_RE`, `_BLOCK_HEADING_RE`, `_INLINE_BOLD_SUB_RE`, etc.). Eliminated the local `_SKIP_OPEN`/`_SKIP_CLOSE` compile-inside-function anti-pattern entirely. All call sites updated to use the pre-compiled forms. Zero functional change; ruff/lint clean; 80/80 tests pass.

## 2026-04-18 - Per-Call Set Allocation and Regex in get_task_prompt() (prompt_registry.py)

**Learning:** `get_task_prompt()` rebuilt `_SHARED_VARS = {"formatting_rules", "formatting_rules_compact"}` as a new `set` on every call, and used `re.findall(r"\{(\w+)\}", text)` without pre-compilation. While not the hottest path, this function composes every task-specific prompt (QnA, synthesis, URL selection, image analysis) — dozens of calls per research session.
**Action:** Hoisted `_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")` and `_SHARED_VARS = frozenset({...})` to module level. Using `frozenset` is also faster for membership tests (`in`) than a `set` literal since it is hashable and interned. Verified: 68/68 tests pass.
