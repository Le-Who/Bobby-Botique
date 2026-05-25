## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.
## 2025-05-25 - Prevent OOM during prompt length calculation
**Learning:** Using `len(str(part))` to calculate length for LLM metrics triggers massive synchronous string allocations if the part is a dictionary containing large base64 image data or binary buffers, blocking the thread and wasting memory.
**Action:** When calculating text length for token estimation or heuristics, strictly use type checking `isinstance(part, dict)` to extract the text length, avoiding generic string conversion on binary payloads.
