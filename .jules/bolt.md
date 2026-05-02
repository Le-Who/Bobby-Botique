## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Replacing sequential queries with CTEs
**Learning:** In handlers like `get_start_menu_content` where multiple database metrics are needed (requests, documents, conversations), sequential database fetches using the wrapper functions increase transaction overhead. Fetching full records (`get_user_documents()`) and using `len()` in python loads data that isn't needed. Using `asyncio.gather()` causes database transaction errors due to shared sessions.
**Action:** Used `get_user_activity_summary` (which utilizes `COUNT(*)` and PostgreSQL CTEs `WITH ... SELECT`) to do a single round trip to the database for multiple separate queries.
