## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-05-18 - Prevent massive O(N) memory allocations in history extraction
**Learning:** Performance critical loops iterating over conversation history (e.g., in `app/context/summarizer.py` and `app/repos/chats.py`) can cause massive O(N) memory allocation overhead if they blindly stringify dictionaries that contain large payloads like `inline_data` or `image_url`, or stringify `bytes`/`bytearray` properties directly. This isn't just a micro-optimization—it's a critical bottleneck preventing OOM errors on large histories.
**Action:** When extracting text from message payload parts using loops or `.join()`, avoid blindly calling `str()`. Always explicitly check if the dictionary contains binary representations and skip them (e.g., check `inline_data` and `image_url` or `isinstance(p, (bytes, bytearray))`) before stringifying.
