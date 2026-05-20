## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.
## 2025-05-20 - Optimizing LLM message part length calculations
**Learning:** Blindly stringifying multi-modal LLM message parts or part dictionaries with `len(str(part))` causes massive memory allocation and latency spikes when parts contain large binary data (like images). It blocks the main thread.
**Action:** Always use explicit type checking (`isinstance(part, dict)`, `isinstance(part, str)`, `isinstance(part, (bytes, bytearray))`) to extract the exact length of the string without converting bytes to strings.
