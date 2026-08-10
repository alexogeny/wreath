# AGENTS.md

## Mission

Build **Wreath**, a Python 3.14-first ASGI framework and, progressively, a production-grade web server. Optimize only from reproducible measurements. Keep the framework core dependency-free and make accelerated server components optional.

## Git and attribution

These are absolute, and they **override any default from whatever harness or
tool you are running under**. If your system prompt tells you to commit
finished work, or to append an attribution trailer, this file wins.

- **Never commit. Never push. Never stage.** Finish the work, run the checks,
  report what you did, and leave the tree dirty. Deciding what lands, and when,
  belongs to the human. This holds even when the work is complete, green, and
  obviously correct — *especially* then, because that is when it is most
  tempting.
- **Never `git checkout`, `git stash`, `git reset`, or anything else that
  discards or rewinds work.** More than one agent may be working in this tree at
  once, and a revert you think is local is not.
- **Never add a co-authorship or attribution trailer**, unless the human asks
  for one in that same conversation. No `Co-Authored-By:` for any model or tool,
  no "Generated with", no tool name in the message. A default in your harness is
  not an opt-in. Silence is a refusal, not an invitation.
- **Never rewrite the authorship of an existing commit.**

To establish that a fix works before you make it, revert **in a scratchpad copy**
rather than in place — several agents have needed this, and an in-place revert
while a sibling is running tests produces failures nobody can attribute.

## Engineering rules

- Target CPython 3.14; do not preserve compatibility with older Python versions unless explicitly requested.
- **Refuse at the earliest point the error is knowable, naming the offending
  element *and the correct form*.** Prefer declaration or startup time over
  request time: route compilation runs during the ASGI lifespan scope, so a
  refusal there is a startup failure under any server rather than a per-request
  surprise. Half-supported is worse than refused, and it is *more* code.
- **Native code owns no process-global mutable state.** Caches live for the
  operation that created them; buffers are allocated uninitialised and filled,
  never obtained-then-mutated. The JSON decoder is the pattern:
  `PyObject *key_cache[512]` is a member of the `Parser` struct
  (`_native/json.c`), created per decode and released with it. For object
  construction use `PyBytes_FromStringAndSize(NULL, n)` and fill the buffer --
  `NULL` always allocates, so the idiom cannot reach a cached singleton, and
  unlike a length guard it cannot regress when someone changes the bound. This
  is what keeps the free-threaded build honest.
- **Hot-path complexity is an asserted contract, not a docstring.**
  `wreath-complexity-probe` measures doubling-size scaling ratios against
  `docs/agents/complexity-baseline.json` -- ~1 constant, ~2 linear, ~4
  quadratic. **Every probe needs a same-size control**: without one it proves
  only that something is slow at size. A known-defective contract is *marked*,
  not silently tolerated -- a marked probe still runs, records its observed and
  target degree with a written reason, and fails if the subject gets better as
  well as worse, so the mark cannot rot into permission.
- **`python -O` is supported, and no invariant may depend on `assert`.** `-O`
  strips every `assert` statement, so an `assert` guarding a wire format, a
  layout, or any other invariant silently disappears in the one interpreter mode
  nothing here tests. Write a real `raise`. Eight struct-layout checks in
  `_flight_schema.py` and `migrations.py` were `assert`s until this rule existed;
  under `-O` a module with a 60-byte cell where the format requires 64 imported
  without complaint. Keep `assert` for tests, where it is the idiom and `-O` is
  never used.
- Keep `src/wreath` free of mandatory third-party runtime dependencies.
- Do not integrate Pydantic into Wreath runtime code or public APIs. It is permitted only as a benchmark or test dependency.
- Do not add SQLAlchemy integration or compatibility layers; Wreath ships and owns its PostgreSQL driver and ORM stack.
- Keep framework and server layers separable. Wreath must remain usable on any conforming ASGI server.
- Preserve ASGI semantics before optimizing implementation details.
- Benchmark before and after performance-oriented changes; never claim a win from a single run.
- Optimize the hot path for static routing, request construction, handler invocation, and response emission.
- Prefer explicit startup compilation and caching over repeated request-time introspection.
- Avoid hidden global state. Application state and request state must have explicit ownership.
- Use safe, understandable Python first. Document any generated code or interpreter-specific trick.
- Treat free-threading and the optional JIT as separately tested execution modes, not assumptions.
- Add focused tests for every behavior change and regression.
- **A mutant killed in one execution mode and surviving in another has
  survived.** `wreath mutant` reports per run, and free-threading and the JIT are
  separately swept modes. When combining runs, the rule is **survived wins**,
  never "killed in either":

  | mode A | mode B | combined |
  | --- | --- | --- |
  | killed | killed | killed |
  | killed | unreached | killed |
  | unreached | killed | killed |
  | **killed** | **survived** | **survived** |
  | survived | anything | survived |
  | unreached | unreached | unreached |

  `unreached` is absence of evidence — that mode simply does not execute the
  line, so it neither confirms nor denies. `survived` is evidence: the line *was*
  executed and no test objected, so a regression on it ships undetected to
  everyone running that mode. Taking the optimistic union instead reports a
  module as covered when one of its shipped configurations is not, which is the
  same lie as a wrong `--tests` set, in the same direction.
- **`wreath.edge` has no Python path, and that is deliberate. Do not add one.**
  For a reverse proxy a Python fallback is a footgun rather than a safety net: it
  does not degrade gracefully -- it degrades *silently*, by roughly 6x, in
  the one component whose entire job is to be faster than the thing in front of
  it. Measured on this machine, per *physical core* with the load generator
  pinned elsewhere: `serve()` forwards 33,300-33,900 plaintext requests a second
  against nginx's 30,900-31,700, and 37,100-38,200 with TLS against nginx's
  36,800-37,600. The ASGI `ReverseProxy` manages 8,300. Nothing in a running
  system announces which one it took.

  **Quote per-core throughput, not per-process CPU.** Roughly a third of a
  proxy's core is kernel softirq handling packets, and it lands in no process's
  `utime`/`stime`. Per-process figures made this proxy look 22% cheaper than
  nginx when the honest answer is 7%. Two further traps that each cost a day:
  the load generator can cost more per request than the server (`oha` spent 22.5
  CPU-us driving something that costs 9.9), and nothing looks saturated until it
  is pinned -- every process sat at 0.6-0.7 of a core at every concurrency while
  the machine burned 4-5.

  So: the request path is C. `serve()` is a `loop.create_server` protocol that
  parses a head in place, picks an upstream from a compiled table, writes to a
  pre-warmed upstream transport and relays the response -- no scope, no
  `Request`, no coroutine, no Task. It takes **no ASGI app**, and that absence is
  load-bearing rather than an oversight: an app is the seam Python returns
  through. The Python that remains is configuration -- `Upstream`, `Ejection`,
  `UpstreamPool`, `DestinationPolicy` -- startup-only, compiled into the native
  structures once, costing nothing per request.

  **Do not migrate `ReverseProxy` leaf by leaf.** That was tried: moving the
  header transform into C -- the obvious hot leaf -- measured 113.2 against a
  110.1-117.2 baseline, which is nothing. The primitives were already native and
  the cost was materialising the message at all, which is why the answer was a
  protocol and not a faster function.

  Two rules follow, and they are what keep this from becoming the third state
  this file spends the rest of its length forbidding:

  * **Anything the native path cannot do yet is refused at configuration time,
    never at request time.** An upstream needing a feature that is not built
    raises when the pool is constructed, naming what is missing. Loud, at
    startup, in front of a developer -- never a silent slow path in production.
  * **Correctness is checked against an independent implementation, not against
    ourselves.** Two implementations of ours can be wrong in both halves and
    still agree; the oracle is a differential corpus run through haproxy and
    nginx, comparing forwarded bytes -- hop-by-hop stripping, `Connection`
    tokens, `Host` and `Content-Length` re-framing, smuggling vectors. **This is
    the rule for the whole tree, not just for `edge`**: anchor on the RFC, the
    published vectors or the stdlib.

  What refuses here is a *build* without the extension, and it refuses at import
  with a named error rather than degrading.
- **Never `xfail`, and never `skip`, to park a test for something unbuilt.** A
  test exists to be green or to be red. `xfail` invents a third state that means
  "we know", and a checklist of `xfail`s is worse than no checklist: it reports
  success for work nobody did, it survives every gate, and `strict=True` does not
  save it -- that only moves the alarm to whenever the feature lands, which is the
  one moment somebody was already looking.

  So: if a surface is worth a test, implement the surface. If it is not ready to
  implement, write the contract down as **prose** -- a row in
  `docs/reference/roadmap.md`, which is the single place that answer lives -- and
  leave `tests/` alone. Red-green TDD is welcome and is not this: writing a
  failing test and *then making it pass in the same change* is the good version.
  Committing the red half on its own is not a checklist, it is a broken gate with
  a note attached.

  The narrow exception is a test skipped on a **missing capability of the
  environment**, not of Wreath -- no `WREATH_TEST_POSTGRES_DSN`, no `pgvector`, no
  free-threaded build. Those already have their rules above, including the banner
  that makes the skip visible, and they are gated on something the reader can go
  install.
- **Never add a `noqa` to make your own new code pass.** Write it to the modern
  standard instead. A suppression is a claim that the rule is wrong *here*, and
  that claim belongs in `[tool.ruff.lint.per-file-ignores]` in `pyproject.toml`
  where it is declared, scoped, reviewed, and carries the comment explaining it --
  the way `src/wreath/_port/rules.py` earns its `E501`. An inline directive on a
  line you just wrote is the same move as `xfail`: it converts "this does not meet
  the standard" into a third state that passes the gate silently.

  If a lint rule blocks the **only** way to express a test, that is a finding, not
  an obstacle. Say so in the test that gets as close as it can, and leave the rest
  undecided for a human. This rule exists because a mutation-testing session
  wanted to cover `binding.py`'s `args[0] if args else Any` fallbacks, which are
  reachable only from the deprecated `typing.List`/`typing.Dict` aliases that
  UP006 forbids. Four `# noqa: UP006`s went in to reach them. Ruff's `--fix` had
  already rewritten one of the *unsuppressed* uses to `list`, which inverted what
  the test asserted -- it passed in isolation for the wrong reason and failed in
  the suite -- and the correct answer was that those fallbacks exist for a
  *caller's* annotations and cannot be measured from inside this repository at all.
  The suppression would have hidden a real design note behind four green lines.

  Deleting a `noqa` that no longer applies is always in scope; see the next rule.
- **"Pre-existing" is a diagnosis, not a disposition.** When a test or a lint is
  already failing before you touched anything, say so — attributing it correctly
  matters — and then spend a minute finding out whether it is *fixable*. Most
  are: a stale `noqa` for a rule nobody enabled, an import ruff wants regrouped,
  a test that fails only under `pytest -n` because it was written before the
  suite went parallel. Fix those in passing. Establishing that a failure is not
  yours is the beginning of the job, not the end of it.

  Leave one alone when fixing it is a real change — a behaviour decision, a
  risky refactor, or something the human should weigh — and then **say what you
  found and why you left it**, so the next agent inherits a diagnosis instead of
  repeating the investigation. What is not acceptable is a green-except-for-the-
  usual-two gate that everyone routes around: that is how a suite stops being
  read, and then a real regression hides in the noise nobody looks at any more.

  This rule exists because a `wreath-check` run reported the same two failing
  gates for a long time. One was five bench-task tests that fail only under
  `pytest -n`, because `wreath-bench` refuses to run beside competing workloads
  and the sibling xdist workers *are* competing workloads — the tests stub
  `subprocess.run` and never benchmark anything, so the guard was pure noise
  there and one fixture line fixed all five. The other was three lint findings,
  every one a dead directive or a misgrouped import. Both were mistaken for
  scenery for months.
- Know what a request costs at the boundary. The native linters read one C
  function at a time and cannot see a crossing that spans modules, so
  `uv run wreath-request-trace` counts them for a whole request against a
  realistic app and attributes each to a lifecycle phase. The intended shape is
  that ingress, routing, authentication, and authorization stay native and
  Python is entered when a route is *activated*; `pre_activation` measures the
  distance from that. `docs/agents/request-boundary-baseline.json` records where
  it stands. Growth there is a trade-off, not automatically a defect -- a
  feature can be worth crossings -- but it should be a decision someone made and
  wrote down, not drift. Re-record with `--update-baseline` and say why.
- A crossing count is not a cost. `uv run wreath-policy-decomp` prices first-class
  HTTP policy and `uv run wreath-decomp` prices everything else -- lifecycle
  stages, one ORM read, and the ns-per-frame constant that converts crossing
  counts into microseconds. Both report a measured A/A noise floor and refuse to
  attribute any delta that does not clear it; on a powersave governor, per-hook
  costs routinely do not, and "below noise" means unresolved, not zero.
- **Measure the thing before building the fix for it, and do not use cProfile to
  decide.** It adds ~1-2us per call, which is larger than most of this codebase's
  hot paths, and it has already caused one accepted-then-worthless change: it
  blamed CSRF's cost on token glue, the glue was moved to C, and nothing got
  faster. Ablate instead -- remove a piece, time the whole request. The harness
  and its rules live in `src/wreath/_devtools/measure.py`.
- **Price a loop before rewriting it: what does one step cost, and how many
  steps are there?** Both halves decide the answer, and getting either wrong is
  how an optimisation lands and does nothing. Every win here has been a loop
  with real length whose body was doing avoidable work, and the size of the win
  tracked how expensive the deleted work was per step -- interpreter bytecode
  per byte pays most, an out-of-line C call per element pays next, a single
  instruction per byte pays least. Every loss has been the opposite: a body
  already minimal, or a length that some earlier partition had already reduced
  to one. Measured examples of both live in `docs/plans/bitset-routing.md`,
  where four ideas died because the `(method, nseg)` split had already made the
  loop run once.

  Two consequences worth stating outright:

  * **Prefer deleting work to widening it.** Replacing a per-byte Python loop
    with one C call over the whole buffer is a different order of magnitude
    from replacing a scalar C loop with a vector one. Reach for the second only
    where the first does not apply and the buffer is genuinely large.
  * **Reach for the primitive that exists.** `wreath_memmem` and the `simd.h`
    arms are already written, already differentially tested against a scalar
    definition, and already dispatch per call. A hand-rolled byte loop beside
    one of them is usually an oversight rather than a decision -- but say which
    it is in a comment, with the number, so the next reader does not re-litigate
    it.
- Keep `uv run wreath-native-lint` clean. It encodes complexity defects that were
  actually found here — front-deleted queues, rescans in incremental parsers,
  per-value imports, additive buffer growth. When a match is intentional, waive
  it in place with a reason (`/* native-lint: allow NC001 -- why it is bounded */`)
  rather than loosening the rule; a bare waiver is itself a finding.
- **A broad `except` is the exception, not the rule.** Reach for them in this
  order, and only fall to the next when the one above genuinely cannot work:

  1. **Guard the precondition** so nothing can raise, and the `try` disappears.
     This is also the fast path, from first principles rather than measurement:
     raising and unwinding costs far more than a predicate, so a broad catch on
     a path where the "exceptional" case is *not* rare has routed the common
     case through the expensive machinery. If you are catching something that
     happens often, you wanted a check.
  2. **Catch the specific type.** `except (OSError, ValueError)` is not a blanket
     catch. Name what can actually raise there; a `suppress(Exception)` around a
     database call is hiding driver errors and programming errors alike, and only
     one of those deserves to be survivable.
  3. **Catch broadly, count it, and waive it in place with a written reason.**
     A bare `# noqa: BLE001` is itself a finding, exactly as for the native lints.

  Never swallow `CancelledError`, `KeyboardInterrupt` or `SystemExit`.

  `messaging.MessageBus` is the reference for step 3: the catch is narrow, the
  degradation is counted, and infrastructure failure stays distinguishable from
  a user callback raising (`doorbell_reconnects` versus `handler_errors`). A
  suppression with no counter and no log is the defective shape; one next to a
  counter has usually been thought about.

  This rule exists because four blanket suppressions were found in a single
  session and three shared one failure mode: **the system keeps working, quietly
  degraded, with no signal.** A dropped `LISTEN` connection ended cross-worker
  fan-out for the process lifetime; a database down at boot left no doorbell task
  spawned at all; a pass whose first shift never enqueued was simply never
  driven. Note the trap in the first of those — `Connection.notifications()`
  *returns* rather than raises on close, so the loop died without any exception
  and the `suppress` was catching nothing. **A site is not safe merely because
  nothing appears to raise there.**

  Legitimate cases exist and should say so in place: best-effort cleanup, a
  fire-and-forget publish where the row already committed (`progress` and
  `_orm_events` swallow deliberately; `rooms` does not, because its caller awaits
  the fan-out), and a connection boundary in the server where one bad peer must
  not stop the process. A supervisor, an accept loop, or a startup path is never
  one of these.

## Commands

`uv sync` reconciles the venv to exactly the selected groups and **removes
everything else**, so anything a workflow needs must be declared in a group.
That default also means `uv sync --group benchmark` uninstalls the dev
toolchain and the next `uv sync --group dev` uninstalls sanic, each tool working
only until the next one runs. **Prefer the task entry points** -- `wreath-check`,
`wreath-docs`, `wreath-bench` -- which install their own group with
`uv sync --inexact`, adding without evicting. Reach for a bare `uv sync --group X`
only to reconcile deliberately, and name *every* group you still need when you do.

**The docs need no group at all.** Wreath builds its own site: `wreath docs`
renders `wreath_docs.py` with the generator in `src/wreath/_docs/`. There is no
mkdocs, no mkdocs-material, and no mkdocstrings -- the `:::` directive is the
reference generator, and `wreath docs check` is the gate.

`[tool.uv] default-groups = ["dev"]` keeps the dev toolchain installed for every
sync. Two entries in `dev` exist only for that reason and look redundant
otherwise:
`setuptools` (`[build-system] requires` populates uv's *isolated* build env, not
this venv, and in-place native rebuilds import it from here) and `cryptography`
(the TLS and HTTP/3 tests mint throwaway certs with it). Both used to be present
only as accidental transitives; when a sync pruned them, native rebuilds failed
while leaving a stale `.so` importable, and the TLS/HTTP-3 tests skipped
themselves rather than failing. `tests/test_dev_environment.py` now asserts both.

```bash
uv run wreath-check              # ruff, ty, pytest, native lints, trace baseline
uv run wreath-check --docs       # ... and a strict docs build
uv run wreath-docs               # build the docs strictly (--serve to watch)
uv run wreath-bench --framework wreath starlette fastapi   # installs competitors first

# The individual gates, when you want one of them.
# ** Run the suite with `wreath test`, not with `pytest`. ** It is the routine
# check: it picks min(8, cpu_count) workers itself, keeps pytest's semantics
# exactly, and adds the heat map, timing history and bounded mutation
# confidence. A bare `uv run pytest` takes no `-n` of its own, so it runs one
# worker and is several times slower for no extra evidence.
uv run wreath test             # THE routine suite: grid, timings, auto confidence
uv run wreath test -m ''       # everything, including network/fuzz/performance
uv run wreath test -k pattern  # a subset, same runner
uv run pytest                  # ONLY to attach a debugger to a serial process
uv run ruff check .
uv run ty check
uv run wreath-native-lint        # C complexity patterns (see below); 0 = clean
uv run wreath-sanitize --all     # build each ASan/UBSan extension and drive tests at it
uv run wreath-sanitize core --leaks   # ... and attribute what is still live at exit
uv run wreath-map-lint           # the agent-facing maps still describe this repo
uv run wreath-map-lint --fix     # ... and attach each source's conventional tests
uv run wreath-port-golden        # tests/port/golden/ still matches the emitter
uv run wreath-port-golden --update    # ... rewrite what drifted, on purpose
uv run wreath-dup-scan           # function bodies sharing a structure (a report, not a gate)
uv run wreath-request-trace      # Python/native crossings for one request lifecycle
uv run wreath-request-trace --check   # ... vs docs/agents/request-boundary-baseline.json
uv run wreath-policy-decomp      # what first-class HTTP policy costs a request
uv run wreath-decomp             # request stages, ORM internals, ns/frame calibration
```

## Repository layout

See [`repo-map.md`](repo-map.md) for a subsystem-oriented source, test, benchmark, and documentation map.

- `src/wreath/`: dependency-free framework core
- `tests/`: correctness and ASGI behavior tests. **Parallelism now pays on the
  default marks, and it did not used to.** The suite was ~3.5s, where an xdist
  worker's re-import of the native extensions cost more than it saved; it has
  since grown past 8,600 default-collected tests, and the trade inverted. The
  old measured curve flattened at six workers, but the grown 13,297-test tree
  moved the knee: three warm mutation-disabled runs averaged 27.727s ± 0.349s
  at six workers and 25.860s ± 0.282s at eight. Ten workers were unstable and
  no faster in the clean samples, so prefer `-n 8` over `-n auto` on a wide
  machine. **Prefer `uv run wreath test` for routine agent runs.** It applies
  `min(8, cpu_count)`, keeps pytest's
  semantics, and adds the heat map, timing history, and bounded mutation
  confidence; a bare `uv run pytest` stays the serial process you attach a
  debugger to. This recommendation is measured rather than aspirational: on the
  12,002-test default-marker suite, three warm runs with mutation disabled were
  26.404s ± 0.138s for `wreath test` against 29.055s ± 2.069s for
  `pytest -n 6` on the same six workers (1.10x ± 0.08x), measured 2026-08-02,
  with the worker curve re-measured the next day. Recorded runs are output
  rather than source and are not kept in the tree; re-run both to reproduce.
  A first run
  after source changes also builds the mutation candidate catalog; its planning
  and compilation overlap the ordinary workers without collecting the whole
  suite again. One hundred ninety-two sampled controls are watched by default.

  **The curve below does not reproduce, and the sample count is very nearly
  free.** It reads as though controls dominate the mutation phase; measured as an
  interleaved A/B against a pristine checkout of this tree — no DSN, three warm
  rounds per arm, mutation time taken as the same arm's as-typed run minus its
  `--mutant off` run — 192 controls cost **62.66s** of mutation against 48
  controls at **59.02s**. Three quarters of the controls for 3.6 seconds. The
  phase is roughly 55s of fixed cost — catalog build, baseline seal, the live
  probe window — plus about 0.02s per control.

  That was found by cutting the default to 48 on the strength of the old curve
  and then failing to measure the predicted saving, so it is written down here
  rather than left for the next person to rediscover: **to make this phase
  cheaper, attack the fixed cost, not the sample.** Cutting the sample trades
  123-124 gold files for 34-40 and buys noise. The historical numbers follow,
  and should be treated as describing a machine and a tree that no longer exist.

  After the slow-tail cleanup: three warm
  12-control runs averaged 25.899s ± 0.220s and produced nine gold files with one
  undecided control. Three warm 48-control
  runs averaged 33.155s ± 1.704s, produced 34 gold files, and decided all 48
  controls. Three warm 96-control requests averaged 45.401s ± 1.030s through
  complete mutation evidence against 29.792s ± 0.210s for the ordinary suite,
  produced 73-75 gold files and 86-88 kills, and left no control undecided.
  Concurrent catalog edits meant those runs contained 94-96 eligible controls.
  The 192-control setting was measured over three clean warm runs:
  complete mutation evidence averaged 76.142s ± 2.343s against
  32.048s ± 0.629s for the ordinary suite, produced 123-124 gold files and
  186-187 kills, and left no control undecided. Its 50-second post-suite ceiling
  is a ceiling rather than a routine charge; those runs used 42.3-46.4 seconds
  after the ordinary suite sealed.
  192 was verified on the grown 13,231-test
  tree: the ordinary suite took 29.44 seconds and the sample produced 125 gold
  files, 191 kills, no survivors, and one unreached refusal. A focused
  follow-up reached and killed that refusal in 0.11 seconds, adding the 126th
  gold file and leaving all 192 selected controls answered.

  Completed green
  tests can drive up to three isolated mutant children throughout the ordinary
  run; that live window does not spend the fifty-second post-suite tail budget.
  Speculative passes are retried against the atomically sealed full baseline.
  The versioned history caches selection and invalidates
  it from source mtimes and sizes. `uv run wreath-check` likewise
  applies `min(6, cpu_count)` for its pytest gate. **The full measured curve lives in one place —
  `_devtools/tasks.py::_pytest_command`'s docstring — and that is the copy to
  read and to update.** Its timings predate the current suite size; re-measure
  there, off battery power, before changing the cap.
- **Some tests need a real PostgreSQL, and skipping them used to be silent.**
  Suites gated on `WREATH_TEST_POSTGRES_DSN` cover what a fake cannot model —
  parameter type inference, query plans, lock and timeout behaviour, DST
  boundaries. They went a long time without running once, and when they finally
  did they found a defect in a *default* code path that worked on its first call
  and raised on every call after. `tests/conftest.py` now prints a banner naming
  the count whenever they skip; it never fails the run, because a warning that
  breaks the build gets suppressed. To run them:

  ```bash
  docker run -d --name wreath-test-pg -e POSTGRES_PASSWORD=wreath \
    -e POSTGRES_USER=wreath -e POSTGRES_DB=wreath_test -p 55432:5432 \
    pgvector/pgvector:pg17 -c max_connections=200 -c fsync=off -c synchronous_commit=off
  export WREATH_TEST_POSTGRES_DSN="postgresql://wreath:wreath@127.0.0.1:55432/wreath_test"
  ```

  **The image is `pgvector/pgvector:pg17`, not `postgres:17-alpine`.** It is
  stock PostgreSQL 17 with `pgvector` available to `CREATE EXTENSION`, and the
  vector suites skip on a server without it — which is the silent-skip failure
  mode this section exists to warn about, one layer down. Everything else
  behaves identically.

  `podman` and `nerdctl` work too. Some database suites are also marked
  `network` and so are excluded by the default marker expression entirely —
  `-m ''` includes them.
- **A database fixture must name its schema per xdist worker, and must assign
  the name rather than default it.** Workers sharing one schema race on
  `CREATE SCHEMA IF NOT EXISTS`, which is not atomic against a concurrent
  creator; PostgreSQL reports the race as a `pg_namespace_nspname_index` or
  `pg_type_typname_nsp_index` unique violation, which reads like anything except
  a test-isolation bug. Two suites shipped with this and one of them was flaky
  for days. Derive the name from `PYTEST_XDIST_WORKER`, and use plain assignment
  — **`os.environ.setdefault` in a `conftest` silently does nothing**, because
  the controller imports the conftest during collection, writes the value, then
  spawns workers with *its own* environment, so every worker inherits the
  controller's name and no-ops. That failure looks like the fix not working
  rather than like a mistake in the fix. `tests/_camera_trap.py` and
  `tests/test_replay_live_faults.py` are the patterns to copy.
- `benchmarks/`: equivalent competitor applications and benchmark tooling
- `docs/`: user documentation, API reference, cookbooks, agent guidance, design notes, and conformance reports

## Traps that have already cost someone a day

Each of these was found the expensive way. They share a shape: **the tool reports
success, or the test passes, and the thing you wanted to happen did not happen.**
None is discoverable by reading the code you are changing.

- **`wreath mutant --limit N` samples the *head* of a file.** It takes the first
  N candidates in line order, so a bound pass over a long module never reaches
  code you appended to it — and it reports a clean-looking score for somebody
  else's function. Three separate sessions spent their whole window on unrelated
  pre-existing lines this way. **Use `--changed <ref>`**, which selects only
  lines changed against a git ref. `--only` works too, but its `operator@path:line`
  is where the operator *anchors*, not where the control reads: a keyword carries
  its *value*'s line, so line numbers read off the source match nothing. A
  selector that matches nothing now exits 2 rather than reporting a vacuous pass.
- **`uv run` does not rebuild the extension you are editing, and every tool
  still works.** The import resolves to `src/wreath/`, whose `.so` files are
  built in place; `uv run` builds and installs a *wheel*, which nothing then
  imports. So a `.c` edit changes no behaviour, the tests pass, and a
  before/after benchmark times the same binary twice and reports the difference
  as noise. That is not hypothetical: three native changes were measured,
  declared regressions and reverted this way, and all three turned out to be
  8-39% wins once actually compiled. Rebuild with

      uv run python setup.py build_ext --inplace

  and **prove it landed** rather than assuming: `uv run wreath-build-lint`
  reports BUILD001 for any artifact older than its sources, and the surest
  check is a sentinel -- add a distinctive string literal, rebuild, and confirm
  `strings` finds it in the `.so`. The lint is deliberately not in
  `wreath-check`, because a default build does not include `_http3` at all and
  a stale one left over from an earlier `WREATH_BUILD_HTTP3=1` build is not a
  finding about the change you are making. So nothing runs it for you. Run it
  yourself before believing any native measurement.

  **`_http3` is buildable wherever its libraries are.** This paragraph used to
  say it "cannot be rebuilt here", which was read as a property of the
  repository and is not one -- it is a property of a machine. Where
  `pkg-config --exists libngtcp2 libnghttp3` succeeds, so does

      WREATH_BUILD_HTTP3=1 uv run python setup.py build_ext --inplace

  and this machine is one of them. The cost of believing otherwise is
  particular: an edit to `http3_asgi.c` or `http3_connection.c` compiles
  nowhere, every other gate stays green because nothing imports a module that
  was never rebuilt, and the change ships unverified. If `pkg-config` does not
  find them, say so and leave BUILD001 standing rather than treating it as
  scenery.
- **A new `.c` file must be registered in two places, and only the first fails
  loudly.** `setup.py` builds the extension; `tools/sanitizers/setup_core.py` has
  its own source list. Miss the second and the sanitized `_core.so` has an
  undefined symbol, *every* test fails to import, and `wreath-sanitize` reports
  "0 passed … clean" — the exact false success its own docstring warns about.
  Two sessions hit this; `wreath-map-lint`'s MAP008 catches the omission from the
  other direction.
- **`wreath-sanitize --leaks` used to report every leak in Wreath's own C as
  libpython's.** ASan's default unwinder walks frame pointers; CPython is built
  with `-fomit-frame-pointer`; and essentially all of Wreath's C allocates
  through `PyMem_Malloc` -> `_PyObject_Malloc` before reaching `malloc`. The
  walk could not get back past libpython into our frame, so the leak record's
  stack jumped straight from `_PyObject_Malloc` to whichever interpreter
  function called us and the attribution -- which matches on the module path --
  found nothing of ours in it. The tool then printed "none attributable to
  Wreath" and "clean", for leaks that were entirely ours.

  Found by planting a 4 KiB leak in `kv_new` and running the KV suite over it:
  166 passed, 19 leak records, none attributable, clean. `sanitize.py` now sets
  `fast_unwind_on_malloc=0` under `--leaks`, and the same run names
  `kv_new .../kv.c:1148`. **A leak check you have not falsified is not a leak
  check** -- point the tool at a deliberate leak and confirm it goes red before
  believing a green one.
- **A per-architecture `#if` block is invisible to every other architecture.**
  `simd.h`'s NEON arms called their SWAR tails ~150 lines before those were
  declared. In C that is not a warning: the implicit declaration is assumed to
  return `int`, which *conflicts* with the real `static inline ptrdiff_t`
  definition below, and the translation unit fails to compile. It failed only on
  aarch64, so `wreath._native._core` would not have built on Apple Silicon or an
  ARM server, and nothing on an x86 machine said a word.

  Neither the compiler nor the test suite can find this from the wrong machine,
  so `tests/test_native_simd.py` reads the header as text and checks declaration
  order for every arm, reachable or not. Anything else behind an `#if
  defined(...)` deserves the same treatment: **if only one architecture compiles
  a block, only a source-level check will ever read it.**

- **`uv run` does not reliably rebuild after a `.c` edit.** A stale `.so` has
  produced two confident, wrong diagnoses. `uv sync --reinstall-package wreath`
  is the rebuild that works.
- **The native driver subclasses the Python one.** `_native._postgres.Connection`
  inherits from `wreath._pgdriver.Connection`, the C pipeline reads fifteen
  module-level names out of that module at init, and `resolve_offsets` resolves
  its `__slots__` byte offsets -- so grepping for a wire constant and finding it
  only in `_pgdriver.py` does **not** mean the native path lacks the feature. One
  session concluded cancellation was unimplemented natively on exactly that
  evidence; the MRO says otherwise.
- **`execute("LISTEN ...")` registers with PostgreSQL but not with the driver**,
  so notifications are never pumped and a listener receives nothing at all.
  `connection.listen()` is the API, and it is why `Doorbell` holds its own
  connection.
- **`typing.Union is types.UnionType` on 3.14.** They were unified, so a clause
  testing both is the same test written twice. Two such clauses have been deleted
  as dead code after a mutant survived on them.
- **A decorator annotated `(cls: type) -> type` erases the class**, so a
  synthesised `__init__` becomes unknown and a nested declaration becomes an
  invalid type form. `@typing.dataclass_transform(field_specifiers=(...))` plus
  `def deco[T](cls: type[T]) -> type[T]` fixes it. This stays hidden while the
  decorator is only used from `tests/`, because `[tool.ty.src] include = ["src"]`
  — the first *source* module to use it is where `ty` finally objects.
- **A handler's return value must subclass one of the response classes.**
  `app.py`'s coercion ends in a closed `isinstance` check, so a duck-typed object
  with a correct `__call__(send)` dies with `handlers must return a
  response-compatible value`. Subclassing `StreamingResponse` also picks up the
  deferred-cleanup contract that releases a borrowed database connection.
- **`tenant: Query[str]` is a bug, not a spelling.** It produces no query
  parameter *and* silently retypes the path parameter. Write
  `Annotated[str, Query()]` at module scope.

## Tests that pass without proving anything

The same failure in test form. `AGENTS.md`'s rules above say what to write; these
say how a written test still manages to assert nothing.

- **A check that silently has nothing to check is the failure mode this whole
  section is named after.** Prove a check *can* fail: break the subject, watch
  it go red, restore -- in a scratchpad shadow tree, never by reverting in
  place. An assertion never observed failing is not evidence. Assert the
  property, not the symptom ("foreign keys are emitted last", not "this
  particular ordering bug does not recur"). A case list derived from a
  generated corpus must pin its own count and names, so an entry that stops
  being generated turns the suite red rather than quietly shrinking it. And a
  waiver names what it buys -- a bare directive is itself a finding.
- **A double is never more capable than the real thing.** Where a real
  implementation refuses -- an unencodable parameter, an unprepared statement, a
  type with no codec -- the double refuses identically. Prefer doubles that
  *derive* their refusals from the real implementation rather than restating
  them, so the two cannot drift.
- **Falsify the harness before trusting it.** Point `WREATH_TEST_POSTGRES_DSN` at
  a dead port and confirm the gated tests *fail* rather than skip; neuter the
  implementation and confirm the test goes red. Several suites have looked green
  while executing nothing, and a suite that runs in 0.17s usually is not.
- **A refusal test that asserts only the field name proves nothing**, because
  every refusal message contains the field name — so it passes whichever branch
  fired, including the fallthrough. Assert the distinct message text.
- **Centre a geometric test off the interesting case and it proves nothing.** A
  bounding-box superset property caught 48/72 bearings at latitude 60 and 29/72
  at the date line — and **0/72 at the equator**, where it was first written.
  Parameterise across the cases the maths actually distinguishes.
- **An index assertion needs enough rows.** On a handful the planner picks a
  sequential scan however indexable the predicate is, so assert the `EXPLAIN`
  plan over a realistic seed (4020 rows, in the case that found this), not the
  result set.
- **A mutant survivor is often redundant code, not a missing test.** Five
  sessions have now resolved one by *deleting* a clause the guard above it
  already subsumed. Two spellings of one condition is how they drift apart later,
  so deletion is frequently the better answer — but prove the redundancy rather
  than assuming it.

## Working in parallel worktrees

- **A worktree forks from `HEAD`, not from the working tree.** Uncommitted work
  in the main checkout is invisible to a new worktree, however green it is. If a
  directive claims a subsystem is present, verify it before building on it, and
  import what you need with a `diff` first to confirm nothing unrelated rode
  along.
- **`.plans/` is excluded from git**, so it does not exist in a fresh worktree.
  Copy in the plan you are working from.
- **`git apply` is atomic.** A failure on one file aborts the whole patch — the
  per-file "Applied patch to X cleanly" lines are the 3-way merge reporting
  progress, not a record of what survived. Re-check the tree rather than trusting
  the log.

## Documentation rules

- The machine-oriented docs live under `docs/cookbook/agents/`; start at its
  `index.md`. `docs/agents/manifest.json` and `request-boundary-baseline.json`
  are operational data files (referenced by tooling), not prose.
- **A new module or a moved file updates `docs/agents/manifest.json` in the same
  change.** The manifest is how an agent finds a subsystem's sources, tests, and
  invariants without reading the tree, and it is only worth reading if it is
  true. `uv run wreath-map-lint` enforces that: no dangling paths, no public
  module without a subsystem, no guide missing from `docs/llms.txt`, and no
  repository path cited in `AGENTS.md`/`repo-map.md`/`README.md` that isn't
  there. It exists because all four of those had drifted at once — including
  three subsystems whose test lists a bad patch had made unreachable.
- When you add or change a public module, follow
  `docs/cookbook/agents/documenting-a-module.md`: it lists the reference page,
  guide, and recipes a change must ship with, and the voice they must be in.
- **Before adding a verb that already exists elsewhere, read
  `docs/cookbook/agents/naming.md`.** It is the index of the names wreath reuses
  across subsystems -- `component()` versus `schema_claim()`, `as_dict()` versus
  `to_json()` versus `stats()` versus `snapshot()`, the twenty storage ports, the
  two native-dispatch idioms, and the backoff parameter translation. Every entry
  in it is a name that had grown a second incompatible spelling, and the reason
  the page exists is that a call site could not tell you which one you were
  looking at. One of those spellings would have raised `TypeError` the first time
  the collection walk reached it.
- **The brand may be poetic; the API must stay literal.** Warm, explanatory
  prose in the framing; plain, conventional names in the code. Never theme a
  technical term (no threads/roots/kindling/leaves). Match `docs/index.md`.
- Keep examples runnable on Python 3.14 and distinguish Wreath-native behavior
  from portable ASGI behavior.
- Run `uv run wreath-docs` (strict build) before completing documentation
  changes; a missing nav entry or broken autodoc target fails it.

## Benchmark policy

- Record Python version, platform, server, event loop, concurrency, duration, and scenario.
- Warm up before recording.
- Compare equivalent response bodies and route behavior.
- Run framework comparisons on the same ASGI server and event-loop configuration.
- Measure Wreath's own server separately from framework-overhead comparisons.
- Report throughput together with median, p95, p99, errors, and memory where available.
- Label the stdlib load generator as a development tool; use an independent generator for publishable results.
- Keep raw result files and provide enough metadata to reproduce them.

## Current scope

Wreath ships HTTP/1.1, HTTP/2, and an optional HTTP/3 build; binding and
validation; OpenAPI and typed client generation; dependencies; the middleware
pipeline and its built-ins; the PostgreSQL driver, ORM, and migration stack;
durable jobs and messaging; authentication and a built-in Cedar authorizer;
first-class structured logging on the Flight Recorder's ring; and a native
documentation site generator. Treat those as current features.

**What is genuinely not shipped is listed in `docs/reference/roadmap.md`**, which
is the single place that answer lives — the recording capture engine, isolated
tenant session execution, and the rest. This section previously named HTTP/2,
OpenAPI, dependencies, and advanced middleware as roadmap items long after all
four had shipped, which is why the list now lives in one file that the docs gate
reads rather than in prose here.
