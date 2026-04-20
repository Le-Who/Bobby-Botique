## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2024-04-20 - Huge memory allocation in loops over history containing large file payload chunks
**Learning:** Blindly stringifying dictionaries/objects as fallbacks for large chat histories containing multi-modal payloads (like `inline_data` or `image_url`) can result in massive string allocation overhead because stringifying bytearrays converts them to huge hex representations. In performance-critical loop iterations over history, `str()` on multi-modal items must be explicitly avoided.
**Action:** When extracting textual parts from multi-modal message histories, explicitly filter out binary content (`bytes`, `bytearray`) and dictionary payloads containing keys like `inline_data`, `image_url`, and `file_data` rather than doing catch-all conversion.
