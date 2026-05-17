## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2024-05-18 - Avoiding memory spikes from len(str()) on dicts
**Learning:** Using `len(str(part))` on dictionary objects or raw bytes (e.g., in LLM message processing) triggers massive memory allocations and latency when the dictionary contains large binary payloads (like base64 image strings or byte arrays).
**Action:** Always use explicit type checking (`isinstance(part, dict)`) to evaluate the length of the `"text"` field specifically, check `isinstance(part, str)` directly for strings, and return 0 for binary/raw byte formats.
