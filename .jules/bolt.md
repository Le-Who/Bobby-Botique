## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-05 - Batching Metrics Fetching
**Learning:** Sequential database queries inside handlers (like fetching request count, document count, and conversation count separately) significantly increase network latency and use more resources, especially when one query (like fetching documents) fetches all rows just to count them in Python using `len()`.
**Action:** Consolidate multiple related metric reads into a single atomic SQL query using subqueries (e.g., `(SELECT COUNT(*) FROM table WHERE...) as count`) to reduce network roundtrips, and always use native database aggregation instead of fetching full records into memory.
