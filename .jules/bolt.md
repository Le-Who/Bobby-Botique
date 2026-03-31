## 2024-03-22 - Rejected micro-optimization
**Learning:** Avoid logging generic Python performance tips (like `+=` vs `"".join()`) in the journal, as they violate the rule against generic learnings. Also, replacing `+=` with `"".join()` for small strings in I/O bound handlers is a micro-optimization with negligible real-world impact. Focus on true bottlenecks like DB N+1 queries or massive data processing where this optimization would actually matter.
**Action:** Be more critical of whether an optimization is a genuine codebase bottleneck or just a generic language micro-optimization before implementing it.

## 2024-05-15 - Unsafe RLS with CTEs
**Learning:** You cannot use `set_config` inside a Common Table Expression (CTE) to safely set Row Level Security (RLS) context for the main query. PostgreSQL does not guarantee that the CTE will be evaluated before the RLS policies on the main query's table scan, leading to unpredictable failures or bypassed security.
**Action:** When optimizing database roundtrips involving RLS context (e.g., `set_user_context`), avoid CTEs. Look for opportunities to reduce sequential queries inside the transaction instead (e.g., using `LEFT JOIN`s or combining `UPDATE` statements).

## 2025-03-31 - Atomic upserts with subqueries in VALUES
**Learning:** Sequential PostgreSQL operations (`SELECT` followed by `INSERT/UPDATE`) like tracking daily metrics can be heavily optimized by combining them into a single atomic query using a subquery within the `VALUES` clause (with `COALESCE` and `EXCLUDED` in the `ON CONFLICT` block). This eliminates unnecessary network latency and avoids race conditions without complex CTEs or transaction locking.
**Action:** When updating database counters or streaks that depend on previous state, avoid application-level `SELECT` + `INSERT` logic and use single atomic upserts.
