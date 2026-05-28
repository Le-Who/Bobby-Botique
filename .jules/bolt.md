## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-04-28 - Consolidating Sequential Database Reads
**Learning:** Sequential database queries inside frequently accessed handlers (like `/start` menu generation) introduce significant latency due to multiple network roundtrips, and fetching full records just to count them (`len(docs)`) causes unnecessary memory allocation. Furthermore, `asyncio.gather()` cannot be safely used for concurrent queries because shared database sessions result in transaction errors.
**Action:** Always consolidate multiple read operations into a single atomic SQL query using subqueries, and utilize native database aggregations (e.g., `COUNT(*)`) to avoid pulling full datasets into Python memory.
