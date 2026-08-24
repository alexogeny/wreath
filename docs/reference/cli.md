# `wreath` CLI

The `wreath` command runs applications (`wreath run`, `wreath dev`), generates
typed clients (`wreath typegen`), queries a running server's telemetry
(`wreath inspect`, a client of the [Inspector](inspector.md) socket), and
replays recordings through the owned pipeline (`wreath replay`). Use
`wreath --help` for the full flag list; run options map onto
[`ServerConfig`](server.md).

`wreath test` runs an unchanged pytest suite behind an animated per-file state
map, then reports duration percentiles, practical 100 ms/250 ms/1 s tail counts,
the Tukey outlier threshold and count, worker utilization, and the
slowest tests. A 192-control `--mutant auto` sample runs whenever the ordinary
run has passing tests, reusing their selected-line coverage and stopping each
killed mutant at its first failure. Baseline failures remain red, are excluded
as killers, and still determine the command's exit status. Mutation candidates
are pink while running, yellow after killing a mutant, and purple when a mutant
survives them. Duration remains in the numeric report and never changes a tile's
colour; the final confidence summary also names how many test files earned
verification.
Broad runs use `--collection auto`: once timing history covers at least 80% of
their test modules, each module is collected by exactly one fresh worker and
the modules are balanced by their newest broad-run cost. Focused, cold-history,
and cross-module `xdist_group` runs keep replicated collection and dynamic load
balancing. `--collection replicated` forces the old xdist shape;
`--collection sharded` forces disjoint conventional Python modules and refuses
when it cannot preserve a cross-module group. It also disables xdist worker
restart so a crashed shard fails closed rather than being replaced under a new
shard id.
`--mutant off|sample|changed|full` and the
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

`wreath new NAME` writes a project that already runs and tests green, and
`--forge github|gitlab|codeberg|forgejo|gitea` adds the CI file that host reads —
lint, tests, and preflight. `wreath ci init --forge ...` writes the same files
into a project that already exists, taking `--forge` more than once for a
repository mirrored to two hosts. Both refuse to write over what is already
there. See [Starting a project](../getting-started/new-project.md).

`wreath replay transport MODULE:APP recording.wtr1` feeds a recorded connection
into the owned HTTP/1 driver; `--inject schedule.wfs1` applies a fault schedule
first. `wreath replay plan MODULE:APP --path /users --method GET` runs a canonical
request through routing, binding, validation, and serialization. Unlike `inspect`
and `capture`, `replay` loads the application because it drives the app's own code
in-process — over fake transports only. See
[Replay and fault injection](replay.md) and the cookbook recipe
[Fuzz your own routes](../cookbook/recipes/fuzz-your-routes.md).

::: wreath.cli
