# ADR 0011. Telemetry evidence ownership

- **Status** Accepted
- **Date** 2026-11-20, recorded from existing behavior

## Observability

The request ID seeds correlation, and an admission or delivery owner's terminal
result outranks a later legacy binary error counter. Existing synchronous
operations keep their own duration accounting outside HTTP or PTB timing.
Persisted metrics totals are display data, so they never become new UPSERT
deltas. A delayed previous-day flush cannot create a current-day streak.
Persisted errors are loaded in chronological order so live appends maintain
the same deque ordering. Histogram values above the final finite bucket are
censored, keeping JSON values finite. Instrumentation errors must leave business
behavior intact. See [metrics](../../app/metrics.py), [request evidence](../../app/observability/request_metrics.py)
and [histograms](../../app/observability/histograms.py).

The metrics query snapshot includes pending events once while commits are
fenced. Shutdown drains producers before closing the consumer, since
cancelling the consumer does not process its pending events.
The metrics module re-exports middleware helpers used by older imports.

Selected-key suffixes are evidence and cannot serve as preview selectors.
Bootstrap credentials register with the scrubber before application events can
echo them. The logging writer handles failures without Python's default
`handleError` dump because that dump can print raw arguments to stderr. Admin
alerts reserve attempt capacity before awaiting transport so an outage cannot
send unlimited failed attempts. See [logging policy](../logging.md), [the
writer](../../app/observability/writer.py) and [alert admission](../../app/admin_alerts.py).

Per-user alert cooldown keeps repeated unauthorized messages from amplifying
administrative alerts. Compatibility city lookup omits exception text and
locals because resolvers handle birth data. The logging function accepts
explicit options for embedders and tests, while standard startup uses the
canonical resolver.

Dashboard health and selected-period totals belong to the bounded canonical
snapshot. Pool presence and mixed-scope totals from legacy SSE do not establish
connectivity or freshness. Configuration assignments cannot establish that a
model executed. Missing timing or load evidence remains missing, and one stalled
source cannot block an independent section or outlive the browser deadline.
See [dashboard data](../../app/web.py), [browser counters](../../app/templates/dashboard.html)
and [operations view](../../app/static/js/dashboard-operations.js).
