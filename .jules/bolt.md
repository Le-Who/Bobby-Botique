## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Consolidating multiple database queries into one atomic SQL call
**Learning:** In handlers where multiple unrelated tables need to be queried (e.g. `get_start_menu_content` fetches request counts, document counts, and conversation counts), executing separate asynchronous queries adds unnecessary network round-trips to the database which spikes the time to fetch menu content.
**Action:** Always consolidate multiple aggregate queries into a single atomic SQL query using subqueries (e.g., `SELECT (SELECT COUNT(*) FROM t1) as c1, (SELECT COUNT(*) FROM t2) as c2`), to avoid the N+1 query problem and minimize latency.
