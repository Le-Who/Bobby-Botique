## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Consolidating N+1 User Stats Queries
**Learning:** Fetching user documents entirely (`await get_user_documents(user_id)`) just to determine `len(docs)` creates a massive memory overhead and blocks the thread during `/start` menu rendering, especially for users with many large documents. Coupled with multiple distinct `await db_query()` calls for requests and conversations, this increases latency.
**Action:** Always consolidate aggregate counts into a single atomic SQL query using subqueries (`SELECT (SELECT COUNT(*)...), (SELECT COUNT(*)...)`), and never fetch full records into Python memory if only `COUNT(*)` is required.
## 2025-05-19 - Using explicit DB COUNTs instead of loading lists
**Learning:** Checking the number of documents via `len(await get_user_documents(user_id))` pulls all user documents into memory as dictionaries every time the stats command is run, wasting massive memory overhead on larger user profiles.
**Action:** Always create and use a dedicated `COUNT(*)` SQL query wrapped in a specific repository method (e.g., `get_user_document_count(user_id)`) rather than fetching entire object lists.
