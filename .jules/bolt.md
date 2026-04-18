## 2026-04-18 - Inline regex patterns in hot-path intent detectors and formatters (cmd_image.py, msg_voice.py, text_format.py)

**Learning:** `check_draw_intent_async` in `cmd_image.py` compiled `_VERB_HEURISTIC`, `_should_auto_route` in `msg_voice.py` compiled `action_pattern`, and `sanitize_html_tags` in `text_format.py` compiled `_TAG_RE` inline. All of these functions execute extremely frequently (per intent check, per voice message, or per formatted output block). Inline `re.compile` forces the `re._cache` lookup, bypassing true zero-overhead evaluation and risking cache eviction under high concurrency.
**Action:** Relocated all static regex patterns (`_VERB_HEURISTIC`, `_VOICE_ACTION_PATTERN`, `_TAG_RE`, `_EMPTY_TAG_RE`) to module level constants to guarantee maximum performance per message. Pattern established: any `re.compile()` within an event handler or utility function triggered per-message must be ruthlessly extracted to module scope.

## 2026-04-18 - Constant Allocation Inside Hot Pre-Filter (intent_router.py)

**Learning:** `_handle_crypto()` rebuilt a 4-entry `_COIN_NAMES` dict on every call. `_extract_currency_pair()` called `sorted(_CURRENCY_CODES.keys(), key=len, reverse=True)` on every fiat query — both produce the same result on every invocation and ran on the path that fires for every user message matching the currency intent.
**Action:** Any `dict` literal, `sorted()`, or other collection construction that produces the same result every time belongs at module level. Always scan hot-path functions (called per user request) for local constant definitions that are candidates for module-level hoisting.

## 2026-04-18 - re.compile() Inside Overflow Handler Function (streaming.py)

**Learning:** `_detect_open_markdown()` called `re.compile(r"^```", re.MULTILINE)` plus three anonymous `re.sub()`/`re.match()` calls — totalling 4 regex compilations — on every invocation. This function fires on every streaming message split (when a response overflows 4000 chars), not once per session. The `re` module caches compiled patterns internally (`re._cache`, 512 slots) but the cache lookup itself has overhead and can evict under load. More importantly, `re.compile()` inside a function body is a clear code smell that signals unintentional repetition.
**Action:** Any regex that is not parameterised (pattern string is a literal) belongs at module level as a compiled constant (`_MD_FENCE_RE`, etc.). The pattern: look for `re.compile(...)` or `re.sub(r"..."` literals inside any function that is called more than once — especially in event-driven or per-message async handlers.
