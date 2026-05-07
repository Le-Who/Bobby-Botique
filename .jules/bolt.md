## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-07 - Consolidating Database Queries
**Learning:** Sequential database queries within request handlers introduce significant network roundtrip latency. Using `asyncio.gather()` to fetch these concurrently causes transaction errors in shared database sessions. In addition, fetching full datasets (like `len(docs)`) into Python memory for mere counting causes massive memory consumption and network overhead.
**Action:** Always use native database aggregation (e.g., `COUNT(*)`) instead of fetching full records into Python memory. Consolidate multiple sequential reads into a single atomic SQL query using subqueries in the `SELECT` clause to avoid network latency and concurrency issues.
