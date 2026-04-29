## 2024-04-29 - Prevent Massive String Allocations on LLM Payloads
**Learning:** Using `str()` on message parts (dictionaries) to approximate length is extremely dangerous when parts can contain raw binary `bytes` or `bytearray` (like image data). Stringifying a dictionary containing a 5MB bytes payload takes over 100ms and allocates a massive string.
**Action:** Avoid blindly calling `str()` on fallback dictionaries or LLM message parts. Instead, explicitly check `isinstance(part, dict)` and evaluate the length of the `text` field, or check `isinstance(part, str)` directly.
