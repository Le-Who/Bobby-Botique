## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-04-28 - Optimizing dashboard activity queries
**Learning:** Sequential database queries inside UI handlers (`get_user_today_request_count()`, `get_user_documents()`, `get_conversation_count()`), and especially using `len(docs)` where `get_user_documents` fetches all document fields (which might include large texts/metadata), incurs severe memory allocations and multiple network round-trips simply to display 3 integer counts on the dashboard.
**Action:** Consolidate multiple scalar/count queries into a single atomic database query via subqueries (`SELECT (SELECT COUNT(*)...), (SELECT COUNT(*)...)`), and always use native `COUNT(*)` rather than fetching records into Python just to measure their length.
