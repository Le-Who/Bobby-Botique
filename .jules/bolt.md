## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2024-05-24 - Database queries optimization in handlers
**Learning:** Sequential multiple database queries in commonly used handlers (like get_start_menu_content) degrade performance. Due to shared DB connections, `asyncio.gather` shouldn't be used either. Using subqueries within a single SQL query provides a much faster and atomic way to read multiple related statistics.
**Action:** Always combine disjoint count/status queries into a single query using subselects to avoid N+1 and sequential DB fetch overhead.
