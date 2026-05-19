## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Consolidate DB queries and avoid `len(docs)` memory overhead
**Learning:** Using `asyncio.gather()` or multiple sequential database calls inside an endpoint handler increases network roundtrips and connection holding time. Fetching all rows to count them (e.g., `len(docs)`) causes severe memory overhead on large datasets.
**Action:** Always use native database aggregation (`COUNT(*)`) and consolidate related summary reads into a single query via subqueries to minimize network latency and memory allocation.
