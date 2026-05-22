## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-24 - Batching user stats DB queries
**Learning:** Fetching separate statistics (requests, documents, conversations) sequentially or via `asyncio.gather` causes unnecessary database roundtrips and potential transaction issues in shared connection pools. Moreover, fetching entire document records into memory just to use `len(docs)` creates significant memory overhead compared to native database aggregation.
**Action:** Always consolidate multiple aggregate queries into a single atomic SQL query using subqueries. Use native database aggregation (e.g., `COUNT(*)`) instead of fetching entire datasets to count their length in Python.
