# The native test engine contract

`wreath test` has two deliberately separate jobs: it provides the test command
and reporting experience, and it chooses an execution engine. Historically the
only engine was pytest. The native engine starts a migration in which pytest is
an oracle and compatibility surface, not an implementation dependency forever.

This page is the contract for that migration. A feature is native only when the
same test has the same collection identity and outcome under both engines. An
unsupported construct is an error at collection time; the native engine never
falls back to pytest after importing part of a suite.

## Engine selection

`wreath test` defaults to `--engine native`. It collects once, balances whole
modules from timing history, forks up to eight isolated workers, and drives an
independent stateless C vectorcall loop in each worker. Activity, history, JSON
reports and mutation confidence share the same report model as the pytest arm.
Each worker streams the identity and outcome of each case through an owned pipe
for timing, history, mutation overlap, and the final report. The controller does
no live rendering, ANSI colouring, or alternate-screen writes while workers run.

`wreath test --engine pytest` remains the explicit compatibility oracle and
debugging path. It retains pytest plugins and xdist semantics.

`wreath test --engine dual` first asks both engines to collect the selected
suite. It refuses to execute when their ordered node-id lists differ. When the
lists agree it executes the suite with both engines and returns failure if their
pass, fail and skip outcomes differ. Dual mode is for hermetic compatibility
corpora: because it runs each test twice, callers must not use it for tests with
external side effects.

The native default never falls back. An unsupported semantic hook or option is
refused during collection and names `--engine pytest` as the explicit form.

## Current native slice

The initial native collector accepts:

- Python files named `test_*.py`, selected as files or recursively through
  directories;
- module-level and `Test*` class methods named `test_*`, with a fresh class
  instance for each case;
- synchronous and async test bodies;
- function, module and session fixtures, including dependency ordering,
  autouse fixtures, yield teardown, class fixtures and parametrized fixtures;
- directory-scoped `conftest.py` fixture discovery;
- the `request`, `monkeypatch`, `tmp_path`, `tmp_path_factory`, `capsys`,
  `caplog`, `recwarn`, and subprocess-oriented `pytester` built-in fixtures;
- `@pytest.mark.parametrize`, including comma-separated names,
  `pytest.param(..., id=...)`, and stacked parametrization;
- `@pytest.mark.skip` and `@pytest.mark.skipif`;
- `pytest.raises`, `pytest.fail`, `pytest.skip`, `pytest.approx`, and
  `pytest.warns` inside test bodies;
- `-k`, `-m`, `-q`, `-x`/`--exitfirst`, `--maxfail`, and `--collect-only`.

Fixture graphs are validated before bodies run. Unknown fixtures, dependency
cycles, a broader-scoped fixture depending on a narrower one, semantic pytest
hooks, plugin declarations and unsupported marks fail with the path and
offending name. Async fixture setup, the async test and async teardown share one
owned event loop; module/session async resources therefore do not leak across
loops.

Unknown pytest command-line arguments fail and name the accepted native options.
Activity history, JSON reports, static final reporting, whole-module sharding and
mutation modes all have native owners. No option is silently ignored.

`wreath mutant` and `wreath test` use the same native engine by default. PEP 669
reachability is attributed around the C loop, so the ordinary native run seals
the mutation baseline without a second pytest session. Only candidate-bearing
files are compiled into a native case image; forked mutant children inherit it
and dispatch exact node IDs through the C loop. Mutation planning begins before
native collection. Completed green tests stream their reachability immediately.
The semantic pool starts at full width; when ten percent of its per-file blocks
finish, native workers begin yielding between cases and mutation receives its
first CPU slot. The split then ramps from 7:1 test/mutation toward 1:7 on an
eight-worker run. One semantic slot remains until the baseline seals, after
which mutation inherits the complete measured cap.
Per-file native case images compiled for live probes remain owned by that
mutation operation and are joined for the sealed tail. Test modules are never
re-imported between those phases; import-time registries therefore see the same
single-import contract as an ordinary native run.

## Collection and outcomes

Files are sorted by repository-relative POSIX path. Tests retain source order.
Parametrized cases use pytest-shaped identities:

```text
tests/test_math.py::test_add[small]
```

Collection completes before execution begins. Syntax errors, import errors and
unsupported constructs therefore cannot leave a partially executed suite.

The native core receives an immutable sequence of `(node_id, callable)` pairs.
It calls each object through CPython's vectorcall API, measures monotonic elapsed
nanoseconds, and returns one record per case. A normal return passes,
`pytest.skip` produces a skip, and every other raised exception fails. The C
module owns no process-global mutable state; every case and result belongs to
one invocation.

Exit status follows pytest's public meanings for the supported cases: `0` for a
green run, `1` when a test failed, `2` for interrupted execution, `4` for invalid
native arguments, and `5` when no tests were collected. Collection and internal
errors use `2` because the suite did not execute to a meaningful test outcome.

## Facade ownership

The compatibility module lives under Wreath and is installed into
`sys.modules["pytest"]` only while native collection and execution are active.
Any previous module is restored exactly afterwards. Wreath does not ship a
top-level `pytest` package, so installing Wreath cannot shadow real pytest.

The facade is an adapter, not the engine. Decorators record inert collection
metadata; assertion helpers implement their documented behavior; scheduling and
outcome classification remain native. Extending the facade requires a dual-mode
contract test first.

## Performance evidence

On a fixed broad default-marker workload, three interleaved warm runs measured
native at **25.903s ± 0.242s** versus pytest at **47.152s ± 1.303s**:
**1.820x end to end**. `perf stat` measured **602.417B ± 1.138B** retired
instructions for native versus **1,064.063B ± 6.423B** for pytest, **43.4%
fewer**. The workload excluded concurrently edited `*_mutation_controls.py`
files and three unrelated dirty-tree baseline/time contracts, identically in
both arms. The exact command, reusable harness, logs and raw
`continued-results.json` live under `~/scratch/wreath/native-default/`.

| Broad suite | Native | Pytest oracle | Native result |
| --- | ---: | ---: | ---: |
| Wall clock | 25.903s ± 0.242s | 47.152s ± 1.303s | **1.820x faster** |
| Retired instructions | 602.417B ± 1.138B | 1,064.063B ± 6.423B | **43.4% fewer** |

That instruction result is close to the removable harness ceiling, not an
invitation to claim the remaining test code as runner overhead. A phase
ablation measured collection at 19.270B instructions and native no-op dispatch
at another 0.011B. Direct versus fixture-wrapped execution over roughly 7,000
real zero-argument cases differed by only 0.144B. The hundreds of billions left
are the test bodies and the Wreath behavior they exercise. Native already
deletes about 150B instructions of pytest worker import and orchestration; even
a zero-cost harness cannot delete the subject under test.

The mutation corpus contains 60 independent guard removals, each killed by its
own test. Seven complete interleaved `wreath mutant` runs measured fully native
baseline plus candidate execution at **0.343028s ± 0.022127s** versus pytest at
**9.680501s ± 0.355707s**, **28.221x end to end**, with identical 60-killed
verdict counts. Native retired **0.728B ± 0.006B** instructions against pytest's
**35.078B ± 0.104B**, **97.9% fewer**. Reproduce it with
`~/scratch/wreath/native-mutant/benchmark.py`.

| 60-control mutation corpus | Native | Pytest oracle | Native result |
| --- | ---: | ---: | ---: |
| Wall clock | 0.3430s ± 0.0221s | 9.6805s ± 0.3557s | **28.221x faster** |
| Retired instructions | 0.728B ± 0.006B | 35.078B ± 0.104B | **97.9% fewer** |

The mutation gain comes from deleting work at four boundaries: one pristine
case image and node index is inherited instead of rebuilt in each child; the
baseline runs in a fork so its module mutations cannot poison later verdicts;
only the mutated top-level definition is compiled rather than all its sibling
definitions; and pidfds plus a bounded pipe replace polling and per-mutant JSON
files on the default first-killer path.

The progressive handoff has its own width control. After one discarded cold
pair, six reclaimed native mutation slots averaged **0.1927s ± 0.0154s** and
**725.99M ± 1.06M** instructions over six runs; eight averaged **0.1869s ±
0.0144s** and **726.17M ± 1.93M**. The roughly 3% wall reduction with effectively
flat instruction counts is why automatic runs reclaim the ordinary runner's
eight-slot cap. The harness and raw results live in
`~/scratch/wreath/mutant-reclaim/`.

These are bounded corpus results, not a claim that every suite is 28 times
times faster. Before changing either default, benchmark cold startup, warm
collection and execution separately against pytest over repeated interleaved
runs and require repository-wide dual verdict agreement.

The static complexity sweep intentionally records the collector's remaining
shapes. Directory walks and module/test accumulation are linear in selected
source and emitted cases; stacked parametrization is linear in the Cartesian
product it must emit; selection-expression index increments are constant work;
and recursive `approx` comparison visits a nested value tree. The genuinely
superlinear name lookups and dual-mode file resolution were replaced with sets
and a per-collection path index before those shapes were acknowledged.
Class collection adds each method's emitted parametrized cases and copies the
usually tiny visible fixture table because class fixture callables bind a fresh
instance plan per method; both costs are linear in the compiled output. Marker
lookup walks the bounded marks declared on one test from nearest to farthest.
Those three scanner findings are acknowledged for those bounds; import-path
front insertion and candidate-file nested comprehensions were restructured
instead of acknowledged.

The newer collector and scheduler loops are likewise startup-bounded work:
ignore globs are bounded by literal conftest declarations, fixture and
import-path scans by one selected module, pytester output matching by the
handful of asserted patterns, and native scheduling by collected files. The
greedy file assignment is intentional longest-processing-time scheduling; it
deletes the slow final-worker tail rather than touching a request path.
