## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Consolidating N+1 User Stats Queries
**Learning:** Fetching user documents entirely (`await get_user_documents(user_id)`) just to determine `len(docs)` creates a massive memory overhead and blocks the thread during `/start` menu rendering, especially for users with many large documents. Coupled with multiple distinct `await db_query()` calls for requests and conversations, this increases latency.
**Action:** Always consolidate aggregate counts into a single atomic SQL query using subqueries (`SELECT (SELECT COUNT(*)...), (SELECT COUNT(*)...)`), and never fetch full records into Python memory if only `COUNT(*)` is required.

## 2025-06-21 - Optimizing Provider Metrics Extraction
**Learning:** Blindly stringifying dictionaries with `len(str(part))` for LLM token estimation is a massive performance trap when parts contain base64 bytes or Image objects. The `str(dict)` call stringifies the entire dictionary, including huge binary data, blocking the event loop and wasting memory.
**Action:** Explicitly extract the text field (`part.get("text")`) when `isinstance(part, dict)` to compute prompt length, avoiding the costly fallback.
