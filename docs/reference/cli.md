# `wreath` CLI

The `wreath` command runs applications (`wreath run`, `wreath dev`), generates
typed clients (`wreath typegen`), queries a running server's telemetry
(`wreath inspect`, a client of the [Inspector](inspector.md) socket), and
replays recordings through the owned pipeline (`wreath replay`). Use
`wreath --help` for the full flag list; run options map onto
[`ServerConfig`](server.md).

`wreath test` runs an unchanged pytest suite behind an animated per-file heat
map, then reports duration percentiles, outliers, worker utilization, and the
slowest tests. A twelve-control `--mutant auto` sample runs whenever the ordinary
run has passing tests, reusing their selected-line coverage and stopping each
killed mutant at its first failure. Baseline failures remain red, are excluded
as killers, and still determine the command's exit status. Purple `▣` tiles are currently under
mutation and solid gold `▰` tiles contain a test that killed one, the grid's highest
state. `--mutant off|sample|changed|full` and the
remaining `--mutant-*` flags configure source, tests, operators, sample size,
per-mutant deadlines, up to three concurrent mutant workers, the non-failing
post-suite tail ceiling, maxfail, and gating. Live probes may use the whole
ordinary test window and stop at its seal; they do not consume that tail budget.
Sample planning and compilation
overlap the ordinary workers without a second full-suite collection. A completed green test may also
probe its control immediately: an early kill can award gold during the suite,
while an early pass is retried after the complete green baseline is atomically
sealed.
`--report` embeds the mutation document. Unknown
arguments pass through to pytest. See the
[testing guide](../guides/testing.md#see-the-suite-while-it-runs) for tile
colours, JSON reports, history, CI behavior, and worker selection.

`wreath replay transport MODULE:APP recording.wtr1` feeds a recorded connection
into the owned HTTP/1 driver; `--inject schedule.wfs1` applies a fault schedule
first. `wreath replay plan MODULE:APP --path /users --method GET` runs a canonical
request through routing, binding, validation, and serialization. Unlike `inspect`
and `capture`, `replay` loads the application because it drives the app's own code
in-process — over fake transports only. See
[Replay and fault injection](replay.md) and the cookbook recipe
[Fuzz your own routes](../cookbook/recipes/fuzz-your-routes.md).

::: wreath.cli
