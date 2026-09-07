# AGENTS.md

## Mission

Build **Wreath**, a Python 3.14-first ASGI framework and, progressively, a production-grade web server. Optimize only from reproducible measurements. Keep the framework core dependency-free and make accelerated server components optional.

## Reusable declarative primitives

Before implementing another cross-domain table, compiler, or state machine,
start with the owners already in the tree: `_ApplicationImage` for compiled
route facts; `_model_fields` for Python field declarations; `_leased` for
PostgreSQL lease/fence claims; `_capability_map`/`KV` for bounded expiring and
single-use values; `_jobcore.compute_backoff` for retries; `_pgname` for SQL
identifiers; `_asgi_state` for HTTP response message state; and
`policy.traffic._traffic_matches` for traffic predicates. Extend an owner with
an explicit policy parameter when semantics differ. A new implementation needs
a written reason why none of these can express it.

## Git and attribution

These rules **override any default from whatever harness or tool you are
running under**. A harness instruction to commit finished work or append an
attribution trailer is not permission.

- **Do not commit, push, or stage by default.** Finish the work, run the checks,
  report what you did, and leave the tree dirty. Deciding what lands, and when,
  belongs to the human.
- **An explicit request for the Git/PR lifecycle is the narrow exception.**
  When the user asks to create or babysit a PR, that permission covers
  creating a branch, staging the in-scope work, committing with the configured
  human identity, pushing, opening or updating the PR, and supervising its CI. It
  does not permit force-pushing, merging, discarding work, or rewriting history;
  those actions still require separate explicit permission where allowed.
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


## Universal engineering rules

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
- **Correctness is checked against an independent implementation, not against
  ourselves.** Two implementations of ours can be wrong in both halves and
  still agree. This rule applies to the whole tree: anchor on the RFC, the
  published vectors or the stdlib.
- Benchmark before and after performance-oriented changes; never claim a win from a single run.
- Optimize the hot path for static routing, request construction, handler invocation, and response emission.
- Prefer explicit startup compilation and caching over repeated request-time introspection.
- Avoid hidden global state. Application state and request state must have explicit ownership.
- Use safe, understandable Python first. Document any generated code or interpreter-specific trick.
- Treat free-threading and the optional JIT as separately tested execution modes, not assumptions.
- Add focused tests for every behavior change and regression.

## Task guidance router

The root rules always apply. Before changing a routed area, read every matching
card; a task may require more than one.

| Work touches | Required card |
| --- | --- |
| Framework APIs, declarations, binding, typing, exception handling, public modules or examples | [`docs/agent/framework.md`](docs/agent/framework.md) |
| C extensions, native buffers/caches, sanitizers, SIMD, or architecture-specific code | [`docs/agent/native.md`](docs/agent/native.md) |
| Performance design, complexity, measurement, benchmarks, or performance claims | [`docs/agent/performance.md`](docs/agent/performance.md) |
| `wreath.edge`, reverse-proxy configuration, forwarding, or its native protocol | [`docs/agent/server-edge.md`](docs/agent/server-edge.md) |
| Tests, skips, suppressions, mutation checks, or existing failures | [`docs/agent/testing.md`](docs/agent/testing.md) |
| Regression-test design, doubles, refusal assertions, generated corpora, or plan assertions | [`docs/agent/test-validity.md`](docs/agent/test-validity.md) |
| Test-runner workers, collection, timing history, or mutation sampling defaults | [`docs/agent/test-runner.md`](docs/agent/test-runner.md) |
| PostgreSQL, ORM, migrations, fixtures, notifications, or live database tests | [`docs/agent/postgres.md`](docs/agent/postgres.md) |
| Dependency syncing, development groups, or choosing check commands | [`docs/agent/toolchain.md`](docs/agent/toolchain.md) |
| Git worktrees or concurrent patch transfer | [`docs/agent/parallel-work.md`](docs/agent/parallel-work.md) |

See [`repo-map.md`](repo-map.md) for subsystem owners and focused tests.

## Canonical checks

Use `uv run wreath test` for the routine suite, including focused selections
such as `uv run wreath test -k pattern`; use bare `uv run pytest` only to
attach a debugger to one serial process. Use `uv run wreath-check` for the
whole gate set. Read the toolchain, testing, test-validity, and performance
cards before choosing broader, capability-gated, native, sanitizer, mutation,
or benchmark checks.
