## 2025-04-28 - Optimizing message text extraction
**Learning:** Blindly stringifying multi-modal LLM message dictionaries (`str(part)`) synchronously converts massive base64 image strings and byte arrays into strings. In large chat histories, this blocks the main thread and spikes memory consumption exponentially since it allocates entirely new giant string buffers just to generate summarizer text.
**Action:** Always implement explicit type checking (`isinstance(p, (bytes, bytearray))`) to skip binary formats, and only extract the `"text"` key from dictionaries rather than using catch-all fallbacks like `str(p.get("text", p))` inside text processing routines.

## 2025-05-18 - Atomic updates with CTE and unnest() for performance
**Learning:** Performing consecutive database statements (like `DELETE`, `UPDATE`, then `INSERT` via `db_execute_many`) creates unnecessary network roundtrips that hurt backend performance.
**Action:** When performing sequential deletes, updates, and batch inserts, consolidate them into a single atomic PostgreSQL CTE query. Use `WITH` for the DML operations and a final `INSERT ... SELECT unnest($X)` to batch-insert arrays, checking `WHERE cardinality(...) > 0` to safely handle empty lists without `NULL` insertion errors.
