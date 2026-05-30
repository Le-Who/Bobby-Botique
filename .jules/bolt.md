## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-15 - Consolidate Multiple Dashboard Queries
**Learning:** Using `asyncio.gather()` to fetch multiple database queries concurrently causes transaction errors in shared database sessions. Additionally, fetching full records into Python memory (e.g., retrieving all user documents just to count them) is highly inefficient and creates memory pressure.
**Action:** Consolidate multiple reads into a single atomic SQL query using subqueries, and always use native database aggregation (e.g., `COUNT(*)`) instead of fetching full records into Python memory.
