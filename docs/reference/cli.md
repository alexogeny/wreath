# `wreath` CLI

The `wreath` command runs applications (`wreath run`, `wreath dev`), generates
typed clients (`wreath typegen`), queries a running server's telemetry
(`wreath inspect`, a client of the [Inspector](inspector.md) socket), and
replays recordings through the owned pipeline (`wreath replay`). Use
`wreath --help` for the full flag list; run options map onto
[`ServerConfig`](server.md).

`wreath test` defaults to `--engine native`, running a pytest-compatible suite
through isolated C-dispatch workers and printing one uncoloured final state
map. It then reports duration percentiles,
practical 100 ms/250 ms/1 s tail counts,
the Tukey outlier threshold and count, worker utilization, and the
slowest tests. A 192-control `--mutant auto` sample runs whenever the ordinary
run has passing tests, reusing their selected-line coverage and stopping each
killed mutant at its first failure. The default `--fuzz auto` also releases a
one-worker fuzz batch once mutation-gold files reach five percent of currently
passed files; `--fuzz off` stops after mutation. Baseline failures remain red,
are excluded as killers, and still determine the command's exit status. The
runner performs no alternate-screen writes, ANSI colouring, repainting, or
animation. Squares represent ordinary terminal states, failures use a
multiplication sign, and an all-stage pass becomes a star. `--grid never` is the
default and only accepted grid mode. Duration remains in the numeric report; the
final confidence summary names how many test files earned verification and how
many finished without mutation evidence. Green is intermediate once mutation is
enabled: the terminal grid contains purple verified squares, pink mutation-miss
crosses, fuzz outcomes, and stars, but no green pass block.
Broad runs use `--collection auto`: once timing history covers at least 80% of
their test modules, each module is collected by exactly one fresh worker and
the modules are balanced by their newest broad-run cost. Focused, cold-history,
and cross-module `xdist_group` runs keep replicated collection and dynamic load
balancing. `--collection replicated` forces the old xdist shape;
`--collection sharded` forces disjoint conventional Python modules and refuses
when it cannot preserve a cross-module group. It also disables xdist worker
restart so a crashed shard fails closed rather than being replaced under a new
shard id.
`--mutant off|on|auto|sample|changed|full` and the
remaining `--mutant-*` flags configure source, tests, operators, sample size,
per-mutant deadlines, explicit mutant worker bounds, the non-failing
post-suite tail ceiling, maxfail, candidate engine, and gating. `--mutant-engine
native` records reachability in the ordinary native workers and dispatches the
sealed candidate set through the inherited native case image. Live probes on
both native and explicit pytest arms may use the whole ordinary test window and
stop at its seal; they do not consume that tail budget.
Sample planning and compilation
overlap the ordinary workers without a second full-suite collection. A completed green test may also
probe its control immediately: an early kill can award gold during the suite,
while an early pass is retried after the complete green baseline is atomically
sealed.
With automatic worker counts, the full test pool starts alone. At ten percent
of completed per-file blocks, the first mutator starts; workers yield between
cases while a three-slot background envelope fills. With automatic fuzzing that
means five testers, two mutators, and one fuzz worker at most; the slow test tail
is never serialized onto one worker. The sealed mutation tail then owns the
complete measured pool.
`--fuzz auto|on` advances every mutation-verified file by running its exact
killing tests under a deterministic hash seed and seed-derived schedule, plus
any explicitly marked `fuzz` cases. A clean fresh-process pass earns the cyan
star; the JSON report still distinguishes schedule-only files from files
carrying purpose-built deterministic input corpora. A survivor on a different
control does not retract positive mutation evidence.
An environment-skipped or mixed fuzz contract is `incomplete`, never passed;
it earns no star without converting an allowed missing capability into a test
failure.
One background slot transfers to live fuzzing at the five-percent gold
threshold. An unfinished live batch stops at seal and the final gold set is
redistributed across the full native worker pool.
`wreath fuzz` is shorthand for the fresh native
test, mutation, and gated-fuzz pipeline. The final grouped report places files
in their terminal test, mutation, fuzz, or starred complete row.
`--report` embeds the mutation document. Unknown arguments pass through to the
selected engine; native rejects unsupported options while pytest retains its
plugin vocabulary. See the
[testing guide](../guides/testing.md#read-the-suite-report) for JSON
reports, history, CI behavior, and worker selection.

`--engine native` selects the dependency-free default:
functions and test classes, sync/async bodies, fixture graphs and scopes,
conftest fixtures, parametrization, common built-ins, skip marks, and assertion
helpers. It imports `pytest` through Wreath's scoped facade and runs compiled
cases through C vectorcall loops. Unsupported semantic hooks and command-line
flags fail at collection rather than falling back. `--engine dual --mutant off` collects and
runs a hermetic supported corpus with both engines and refuses any identity or
outcome drift. The full boundary and default-switch criteria are in [the native
test engine contract](../internals/native-test-engine.md).

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
