## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Avoiding len(str()) on dicts
**Learning:** Calculating prompt length using `len(str(part))` on dictionaries triggers massive memory allocations and latency if the dictionary contains large binary payloads.
**Action:** Use explicit type checking `isinstance(part, dict)` to evaluate the length of the `text` field specifically, check `isinstance(part, str)` directly, and return 0 for raw byte formats.
