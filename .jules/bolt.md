## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-23 - Optimizing multiple reads with subqueries
**Learning:** Sequential execution of database queries, particularly when aggregating multiple metrics like counts across tables, introduces significant network overhead. Additionally, fetching full records into Python memory just to compute lengths (`len(docs)`) wastes CPU and memory resources.
**Action:** Instead of sequentially calling individual repo functions or loading full rows, consolidate read operations into a single atomic SQL query using subqueries with native database aggregation (`COUNT(*)`), reducing both network roundtrips and memory overhead.
