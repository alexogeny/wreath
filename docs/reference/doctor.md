# `wreath.doctor`

Diagnosing defects a green test suite cannot see. Today that is the N+1 query:
[`NPlusOneGuard`](../guides/n-plus-one.md) fails a development request at the
query that crossed the line, and `diagnose_n_plus_one` reads the same finding
back out of a running server's recorded traces through its Inspector — which is
what `wreath doctor n-plus-one <socket>` does.

::: wreath.doctor
