## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-15 - Consolidating start menu database queries
**Learning:** `get_start_menu_content` was loading all of a user's documents into Python memory via `get_user_documents` simply to calculate the `len()` of the array, alongside separate queries for request count and conversation count. This created unnecessary N+1 queries, network overhead, and massive memory consumption if a user has many large documents.
**Action:** Consolidate multiple database reads in handlers into a single atomic SQL CTE or subquery approach using native database aggregation (e.g., `COUNT(*)`) to avoid fetching large full records into Python memory.
