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
- A crossing count is not a cost. `uv run wreath-tape-decomp` prices the global
  middleware tape and `uv run wreath-decomp` prices everything else -- lifecycle
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

# The individual gates, when you want one of them
uv run pytest                 # the default marks, serially; use -n 6 for normal checks
uv run pytest -m '' -n 6      # everything, including network/fuzz/performance
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
uv run wreath-tape-decomp        # what the global middleware tape costs a request
uv run wreath-decomp             # request stages, ORM internals, ns/frame calibration
```

## Repository layout

See [`repo-map.md`](repo-map.md) for a subsystem-oriented source, test, benchmark, and documentation map.

- `src/wreath/`: dependency-free framework core
- `tests/`: correctness and ASGI behavior tests. **Parallelism now pays on the
  default marks, and it did not used to.** The suite was ~3.5s, where an xdist
  worker's re-import of the native extensions cost more than it saved; it has
  since grown past 8,600 default-collected tests, and the trade inverted. The
  last measured curve flattened at six workers and turned back up once workers
  outnumbered the cores they shared with the extensions each one loaded, so
  prefer `-n 6` over `-n auto` on a wide machine. `uv run wreath-check` applies
  `min(6, cpu_count)` for you; a bare `uv run pytest` stays serial, because that
  is the one you attach a debugger to. **The full measured curve lives in one place —
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
