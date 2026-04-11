## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-05-18 - Avoiding O(N) allocation in message extraction
**Learning:** When extracting text from message payload parts using list comprehensions and `.join()`, blindly calling `str()` on fallback dictionaries (e.g., `str(p.get("text", p))`) or simply `str(p)` for dicts without a `text` key can trigger massive O(N) string allocations. This happens because some dictionaries contain huge binary payloads (like `"inline_data"`, `"image_url"`, or `"file_data"`). Converting these to strings creates massive memory allocation overhead and spikes latency.
**Action:** Always explicitly check if a dictionary contains binary keys before stringifying it as a fallback. Only convert structures containing plain text or structured text to avoid performance regressions in performance-critical loops (like conversation history extraction in `app/repos/chats.py` and `app/context/summarizer.py`).
