# AGENTS.md

## Mission

Build **Wreath**, a Python 3.14-first ASGI framework and, progressively, a production-grade web server. Optimize only from reproducible measurements. Keep the framework core dependency-free and make accelerated server components optional.

## Engineering rules

- Target CPython 3.14; do not preserve compatibility with older Python versions unless explicitly requested.
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

## Commands

`uv sync` reconciles the venv to exactly the selected groups and **removes
everything else**, so anything a workflow needs must be declared in a group.
That default also means `uv sync --group benchmark` uninstalls mkdocs and
`uv run --group docs` uninstalls sanic, each tool working only until the next
one runs. **Prefer the task entry points** -- `wreath-check`, `wreath-docs`,
`wreath-bench` -- which install their own group with `uv sync --inexact`, adding
without evicting. Reach for a bare `uv sync --group X` only to reconcile
deliberately, and name *every* group you still need when you do.

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
uv run pytest                 # the default marks, serially (~31s); use -n 6 (~8s)
uv run pytest -m '' -n 6      # everything, including network/fuzz/performance
uv run ruff check .
uv run ty check
uv run wreath-native-lint        # C complexity patterns (see below); 0 = clean
uv run wreath-sanitize --all     # build each ASan/UBSan extension and drive tests at it
uv run wreath-sanitize core --leaks   # ... and attribute what is still live at exit
uv run wreath-map-lint           # the agent-facing maps still describe this repo
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
  since passed 4,400 tests and 30s, and the trade inverted. Measured on 12 cores,
  best of two runs: serial 30.7s, `-n 2` 16.8s, `-n 4` 9.8s, **`-n 6` 8.1s**,
  `-n 8` 8.1s, `-n 12` 9.5s. The curve flattens at six and turns back up once
  workers outnumber the cores they share with the extensions each one loads, so
  prefer `-n 6` over `-n auto` on a wide machine. `uv run wreath-check` applies
  `min(6, cpu_count)` for you; a bare `uv run pytest` stays serial, because that
  is the one you attach a debugger to. Re-measure before changing the cap.
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

The first milestone is a small, correct ASGI core and a reproducible baseline. HTTP/2, production server hardening, validation, OpenAPI, dependency injection, and advanced middleware are roadmap items—not implied current features.
