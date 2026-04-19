## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-05-24 - Avoiding O(N) allocation on dictionary stringification
**Learning:** In highly-frequented loops (like extracting text from message history), blindly applying `str()` to a fallback dictionary (e.g. `str(p)`) that contains large binary representations (like Base64 strings in `inline_data` or `file_data`) causes a massive O(N) string allocation overhead per message part. This causes significant performance degradation and memory bloat.
**Action:** Always verify the data structure of parts in message arrays. Use explicit type and key checks to ensure that massive binary payloads are explicitly skipped rather than lazily passed to generic stringification functions.
