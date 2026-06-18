## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Consolidating N+1 User Stats Queries
**Learning:** Fetching user documents entirely (`await get_user_documents(user_id)`) just to determine `len(docs)` creates a massive memory overhead and blocks the thread during `/start` menu rendering, especially for users with many large documents. Coupled with multiple distinct `await db_query()` calls for requests and conversations, this increases latency.
**Action:** Always consolidate aggregate counts into a single atomic SQL query using subqueries (`SELECT (SELECT COUNT(*)...), (SELECT COUNT(*)...)`), and never fetch full records into Python memory if only `COUNT(*)` is required.

## 2025-05-24 - Avoiding len(str()) on multimodal payloads
**Learning:** Calculating token or prompt length by blindly invoking `len(str(part))` on multimodal LLM payloads (which can include `bytes`, `bytearray`, or PIL `Image.Image`) creates massive memory overhead and blocks the main thread.
**Action:** Use an explicit helper function `_get_len(p)` to check types, handle strings natively, extract `"text"` from dictionaries, and immediately return `0` for binary/image types to bypass string conversion entirely.
