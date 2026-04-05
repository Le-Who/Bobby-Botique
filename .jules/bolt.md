## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).
## 2024-06-03 - O(N) String Allocation on Multimodal Parts
**Learning:** Calling `str()` on generic dictionaries during message extraction can lead to massive O(N) memory allocation and CPU spikes if those dictionaries represent multimodal payloads (like base64 `inline_data` or `image_url` objects).
**Action:** When extracting text from conversational history, always explicitly check for and skip binary objects (`bytes`, `bytearray`) and dictionary payloads containing multimodal fields (`inline_data`, `image_url`, `file_data`) before attempting to stringify the object.
