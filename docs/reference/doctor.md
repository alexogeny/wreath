# `wreath.doctor`

Diagnosing defects a green test suite cannot see and contracts a diff can.

**The route/security contract.** `route_manifest` returns deterministic,
JSON-compatible route metadata and `render_route_manifest` emits its canonical
form. `wreath doctor routes <target>` prints it, `--write PATH` records it, and
`--check PATH` makes contract drift a CI failure.

**The N+1 query.** [`NPlusOneGuard`](../guides/n-plus-one.md) fails a development
request at the query that crossed the line, and `diagnose_n_plus_one` reads the
same finding back out of a running server's recorded traces through its
Inspector — which is what `wreath doctor n-plus-one <socket>` does.

**What one request caused.** `find_work_with_trace` takes a trace id and returns
every job, durable bus message, workflow instance and chunked pass carrying it,
because all four record the enqueuing request's `traceparent` on their own
durable row; `find_requests_with_trace` reads the other end of the chain — the
recorded request itself — off the Flight Recorder's ring over the Inspector
socket. `wreath doctor trace <id>` runs both. See
[Observability](../guides/observability.md) for what the propagation means.

`TraceLookup.omitted` is the half to read. Every source the lookup could *not*
reach is named in the same report as the findings — a table not on this database,
a schema still on the version before propagation, no socket, a trace aged out of
the ring, and ephemeral bus messages, which carry no context at all. A forensic
tool that quietly leaves a source out is worse than one that answers nothing,
because "no durable work carries this trace" then reads as "nothing does".

::: wreath.doctor
