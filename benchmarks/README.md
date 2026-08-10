# Benchmark suite

This directory holds equivalent applications for Wreath, Starlette, FastAPI,
Sanic, BlackSheep, Django, and Flask, and the tooling that measures them side by
side.

A benchmark is easy to lie with and easy to be fooled by, so this suite is built
to make honesty the path of least resistance. It reports medians *with their
run-to-run range*, because a scenario's variation is routinely larger than the
gap being argued about. It crowns a winner only when the leader's worst sample
still beats the runner-up's best. It verifies that work was actually done —
counting completed background tasks, checking row counts, reading mutations back
from the database — so no framework can look fast by quietly doing less. And it
is honest about its own limits: the bundled load generator shares a machine with
the server, which makes it a fast tool for sensing direction, not published,
independently-generated proof. Where a comparison isn't quite like-for-like — a
different template engine, an ORM that lacks a feature, a WSGI adapter in the
path — the report says so, rather than letting a green cell imply more than it
should.

## Quieting the machine first (`--quiet`, Linux only)

A desktop is a hostile place to measure. `wreath-bench --quiet=TIER` removes the
noise in tiers, and **the tier you want is the lowest one that gets the spread
you need** — which is a measurement, not a preference. `wreath-bench-quiet
--measure-noise` reports the machine's current A/A spread so you can decide with
evidence.

| Tier | Needs | What it does |
| --- | --- | --- |
| **0** (default) | nothing | Whole-physical-core split between server and generator, niceness, ASLR off for the children. |
| **1** | root | CPU governor to `performance`, turbo off, transparent huge pages to `madvise`, `perf_event_paranoid`, a *named* list of background services stopped, *named* heavy applications frozen, and running containers paused. |
| **2** | root, opt-in | Also freezes every *other* transient application scope, named or not. |

Tier 1 carries the named lists and tier 2 carries the sweep. That split is what
lets an operator audit the plan before granting root: a browser and an idle
Postgres container are exactly as noisy as a file indexer, so they belong beside
the named service list rather than behind a broad freeze that the measurements
say nobody needs.

### Containers

Running containers are **paused**, not stopped. A paused container burns no CPU,
unpauses in milliseconds, cannot lose data, and — unlike stopping — does not
destroy a container started with `--rm`. The plan names the action it is about to
take, and the `--rm` guard stays on the stop path so selecting
`WREATH_QUIET_CONTAINER_ACTION=stop` cannot reintroduce that hazard: a container
whose `--rm` flag is set, *or cannot be determined*, is skipped with its reason
printed.

### Competing workloads

Before any tier — including tier 0, which changes nothing — the tool looks for
other test runs, load generators, benchmark invocations, and processes running out
of this repository or a sibling worktree, and **refuses** if it finds any.

```bash
wreath-bench-quiet --check-competing    # list them and exit 1, or report idle
wreath-bench --allow-competing ...      # measure anyway, and label the result
```

A number taken alongside four agents measures the agents too. Association with
the repository is decided from `/proc/<pid>/exe` and `/proc/<pid>/cwd`, never
from a command-line match — a process launched as `.venv/bin/python` from the
repository root has no absolute path in its command line at all, so a substring
check reports an idle machine while an agent is running. Idle shells are ignored:
being *in* the repository is not the same as *working*, and a check that fires on
every terminal gets overridden by habit.

Measured on a 6-core SMT machine at `powersave`, five runs per arm:

| arrangement | A/A spread |
| --- | --- |
| unpinned, all 12 CPUs | 4.43 – 14.46 % |
| **pinned to one whole core** | **2.70 – 5.46 %** |
| pinned to one SMT thread | 4.26 – 19.18 % |

Two things fall out of that. Tier 0's whole-core split cuts the worst case by
roughly 2.6×, so most of the available quiet is free. And pinning to a single
SMT *thread* is **worse than not pinning at all** — the sibling thread stays
available to everything else on the machine, so the benchmark ends up sharing a
core's execution resources with whatever is running. Disjoint *logical CPUs* is
not the property you want; disjoint *physical cores* is.

### Safety

Tier 2 suspends processes rather than killing them, so a thaw restores the
desktop exactly as it was. Three properties make it safe to run on a machine you
are sitting in front of:

* **The restorer is armed before the first change.** A detached systemd timer
  restores everything after a deadline whether or not the benchmark survives.
  `--quiet` *refuses to change anything* if that timer cannot be armed and
  verified — a change the tool cannot guarantee to undo is one it will not make.
* **The benchmark's own ancestry is exempt.** On GNOME the benchmark runs inside
  the terminal's own `app.slice`, so a naive "freeze the user session" takes the
  operator's shell with it. Every ancestor cgroup is excluded by walking up from
  `/proc/self/cgroup`.
* **Session infrastructure is excluded too**, which ancestry alone does not
  cover: the terminal survives a frozen `dbus.socket` for exactly as long as it
  takes to make its next D-Bus call. Only transient *application* scopes are
  ever frozen, never services, sockets, the session manager, the keyring, or the
  ssh-agent.

Tiers 1 and 2 **dry-run by default**: they print every change with its current
value and the shell command that would make it, and do nothing. Add
`--quiet-apply` once you have read the plan.

```bash
wreath-bench --quiet=1                  # print the plan, change nothing
wreath-bench --quiet=1 --quiet-apply    # apply it, benchmark, restore
wreath-bench-quiet --restore            # undo, from any shell, after a crash
```

Every change is journalled to `/tmp/wreath-quiet.json` before it is applied, so
`--restore` works from a fresh shell. A reboot restores all of it anyway.

## Development comparison

The canonical command is **`uv run wreath-bench`**. With no arguments it pins the
whole process tree to the P-cores (off the slow E-cores), runs the framework
matrix three times so the report carries run-to-run ranges, then runs the webhook
microbenchmarks and the database battery (ORM competitors, PostgreSQL webhooks,
and the full request lifecycle — each behind a throwaway podman container), and
combines the matrix passes and lifecycle rows into one report at
`benchmark-results/full-battery.html`. It forwards unknown flags to
`benchmarks.run`, and whenever `wreath` is selected it also runs `wreath-native`
(wreath's own HTTP+JSON stack — the headline wreath number; the `wreath` arm is
wreath on uvicorn/httptools, kept as the common-server overhead comparison and
labeled by its `server` column).

```bash
uv run wreath-bench                       # full battery: pinned, 3 passes, db + lifecycle
uv run wreath-bench --matrix-only         # just the framework matrix (no webhook/db benches)
uv run wreath-bench --no-db --passes 1    # matrix + in-process webhooks, one pass, no podman
uv run wreath-bench --pin none            # do not pin to the P-cores
```

To run only the framework matrix directly (what `wreath-bench --matrix-only`
wraps):

```bash
uv sync --group benchmark
uv run python -m benchmarks.run
```

By default, the full matrix gives Wreath, Starlette, FastAPI, Sanic, and BlackSheep 1,000 measured requests per scenario. Django and Flask use a 100-request traditional-framework tier so the suite remains responsive. Every scenario receives exactly 10 warmup requests at concurrency 4. Raw JSON and a self-contained HTML report are saved under `benchmark-results/`. End-to-end charts are normalized to 100,000 requests so tiers remain comparable.

ASGI and adapted WSGI applications run behind the same Uvicorn configuration. Sanic runs on its native server because current Sanic releases do not expose the application as a conventional third-party ASGI server target; every result records its server. `--loop auto` uses uvloop when available, and `--http auto` uses httptools when available; the `wreath-native` server follows the same auto loop selection so the native-deployment comparison runs on the same event loop policy as the uvicorn-hosted frameworks. Explicitly select `asyncio`/`h11` or `uvloop`/`httptools` when comparing execution modes. Each result row records the exact server (and loop for `wreath-native`).

Scenarios:

| Name | Route | Behavior |
| --- | --- | --- |
| `plaintext` | `/` | Return `hello, world` |
| `json` | `/json` | Serialize `{"message":"hello"}` |
| `parameter` | `/users/42` | Match one dynamic segment and serialize it |
| `middleware-noop` | `/middleware/noop` | Wreath-only compiled no-op route middleware overhead |
| `missing` | `/definitely-missing` | Wreath-only definite route miss and 404 emission |
| `auth-missing` | `/auth/profile` | Wreath-only protected route with no credentials |
| `auth-authenticated` | `/auth/profile` | Wreath-only bearer authentication and identity exposure |
| `auth-rbac-allow` | `/auth/admin` | Wreath-only RBAC allow path with decision-router pruning |
| `auth-rbac-deny` | `/auth/admin` | Wreath-only RBAC deny path with decision-router pruning |
| `header-lookup` | `/headers` | Read a request header and return its value |
| `body-1k` | `POST /body` | Receive a 1 KiB body and return its length |
| `json-body` | `POST /json-body` | Decode and re-encode a small JSON document |
| `response-64k` | `/response-64k` | Emit a 64 KiB bytes response |
| `stream-4x256` | `/stream-4x256` | Emit four 256-byte streaming chunks |
| `background-noop` | `/background-noop` | Schedule one response-bound background task per request (wreath/wreath-native/starlette); counters verified and drained |
| `background-yield` | `/background-yield` | Response-bound background task that yields to the loop once before completing |
| `routing-shallow-get` | `GET /status/leaf-9995` | Shallow static leaf |
| `routing-versioned-post` | `POST /api/v1/items/category-5/leaf-9996` | Versioned, medium-depth path |
| `routing-trailing-put` | `PUT /api/v2/groups/group-9997/members/` | Trailing-slash route |
| `routing-params-patch` | `PATCH /tenants/acme/collections/collection-9998/items/42` | Multiple path parameters |
| `routing-deep-delete` | `DELETE /api/internal/v3/regions/region-9999/zones/primary/nodes/current/status` | Deep static route |
| `ws-echo` | `/ws-echo` | WebSocket: upgrade once per connection, then measured 125-byte masked text echo roundtrips |
| `template` | `/template` | Render a 20-row HTML table with escaping (Wreath templates; competitors use Jinja2 with autoescaping) — wreath, wreath-native, starlette, fastapi, sanic, blacksheep |
| `cache-control` | `/cached` | Return a response with a validated `Cache-Control` header (Wreath's `CacheControl`; competitors set the header) — same ASGI-tier frameworks. No other scenario sets caching headers |
| `webhook` | `/webhook` | Verify a signed inbound HMAC-SHA256 webhook and respond — wreath/wreath-native only (competitors have no webhook primitive) |

`benchmarks/bench_request_pipeline.py` separately compares legacy probe/rematch routing with single-pass classification for public, missing, and protected routes across repeated in-process trials. Its JSON output records the runtime, platform, route count, iteration count, and raw trial values.

## Consumer type generation

`benchmarks/bench_typegen.py` decomposes a `wreath typegen` run into its phases —
app construction (an import proxy for a synthetic in-process app), canonical
model construction (route and type-hint inspection), target planning, pure
rendering, and filesystem write — across synthetic apps of increasing size
(`small`/`medium`/`large`/`stress`).

```bash
uv run python -m benchmarks.bench_typegen \
    --shape small medium large \
    --output benchmark-results-typegen/latest.json
```

Each shape records per-phase median/p95 (ms), an A/A noise floor on the render
phase, output byte count, MiB/s, peak RSS, and the pure/native output SHA-256
(native is `null` until the gated C renderer is built). Retain raw runs under
`benchmark-results-typegen/`; the baseline lives in
`benchmark-results-typegen/baseline/`.

**Native gate decision (pure only).** The retained baseline shows model
construction and type inspection dominate every shape — at the `large` shape
(1,000 routes) canonical model construction is ~120 ms while pure rendering is
~21 ms (~6% of total), and even at `stress` (10,000 routes) rendering is ~11% of
a ~2.2 s command. Rendering scales approximately linearly (~35–48 MiB/s). Because
rendering is not a material fraction of total command time, the native C renderer
is **not justified**; Wreath carries only the Python renderer. Reopen the
gate only if a retained decomposition shows rendering dominating, and require the
the rendered SHA-256 values to match before comparing any timings.

## Logging

`benchmarks/bench_logging.py` runs the six measurements
`docs/plans/first-class-logging.md` owed, one per `--suite`: `emit` (a record
against stdlib `logging` and structlog), `disabled` (what a *disabled*
`DEBUG(...)` call costs — the load-bearing one, because failure-triggered
logging assumes verbose instrumentation is affordable), `publish` (a LOG cell's
ring publish against a COMPLETION cell's), `drain` (projector throughput as the
log-to-completion mix shifts), `request` (an in-process ablation of the whole
request path), `memory` (whether `MemoryBudget.logging`'s per-entry constants
resemble reality), and `e2e` (the same request over a socket, off by default).

```bash
uv sync --inexact --group benchmark    # structlog is the competitor
uv run python -m benchmarks.bench_logging --suite all \
    --label "what this run measured" \
    --output benchmark-results-logging/latest.json
```

Two failure modes are specific to logging and both are checked rather than
assumed. **A limiter that starts dropping makes its arm fast**: the default
policy passes the first 100 records from a site per second and then one in 100,
so a loop emitting millions from one site measures the drop path. Every emit arm
carries a counting sink and an integrity pass verifies the count; the drop path
gets its own arm, labelled as such. That check earned its place immediately — it
caught an arm built on a WARN site, which `LogSamplingPolicy.ceiling` never
samples. **A ring that fills makes its arm fast too**, for the same reason, so
each native arm sizes its ring from the run's own record budget and refuses the
result if a single record was lost.

The baselines were measured 2026-07-28, before and after `wreath_nfr_log`.
Recorded runs are output rather than source and are not kept in the tree --
`docs/plans/first-class-logging.md` carries the tables and what they mean; the short
version is that the fast tier was not fast until the emitter moved to C, and the
cost has since moved off the request path and onto the projector thread. Every
number is one machine — reproduce before quoting an absolute.

## Native GIL contention

`benchmarks/bench_native_gil.py` separates uncontended kernel latency from
cross-thread GIL exclusion. Its observer is an independent CPU-bound Python
thread; it is intentionally not an event-loop fairness test. Run it as a module
so the repository's `tests`/benchmark imports resolve consistently:

```bash
uv run python -m benchmarks.bench_native_gil \
  --sizes 65536,1048576,16777216 --warmup 5 --trials 15
```

The WebSocket integrity check runs before timing. Retain every trial and use an
A/B/A sequence when experimenting with GIL release: progress in the observer is
a concurrency result, while delayed GIL reacquisition is caller latency, and
both must be reported.

## Response-bound background tasks

`benchmarks/bench_background_tasks.py` is a focused in-process benchmark for response-bound background work. It measures the complete ASGI invocation (not just task-object construction) across the current raw callback, one `BackgroundTask`, an async task that yields, ordered groups of 1/4/16 tasks, synchronous thread-offloaded work, and the streaming and native one-shot integration points.

```bash
uv run python -m benchmarks.bench_background_tasks \
    --output benchmark-results-background/latest.json
```

Arms are interleaved and an A/A control (`no-background` entered twice) fixes the noise floor, so deltas below it are reported as `BELOW NOISE` rather than zero. The task wrapper's incremental cost is read against the `raw-callback` arm, not only against `no-background`. Each arm carries a completion counter: an integrity pass drives every arm a fixed number of times and requires completed tasks to equal `requests * tasks-per-request`, aborting the run if any task is dropped or leaked. Synchronous arms (`sync-noop`, `sync-work`) are thread-offload measurements and must not be compared as equivalent to async no-op work. Groups should scale roughly linearly with task count; investigate superlinear behavior before accepting it. The JSON records Python version, platform, event loop, iteration/warmup/round counts, per-arm median/p95, raw samples, and per-arm completion counts. Retain runs under `benchmark-results-background/`; the retained baseline lives in `benchmark-results-background/baseline/`.

### End-to-end task-completion verification

The `background-noop` and `background-yield` scenarios extend the development comparison harness to `wreath`, `wreath-native`, and `starlette` using each framework's native response-bound background API. Each app maintains process-local counters (`started`, `completed`, `failed`, `inflight`, `max_inflight`) owned by the benchmark, and exposes an unmeasured `/background-stats` endpoint that never joins the timed samples.

```bash
uv run python -m benchmarks.run \
    --framework wreath wreath-native starlette \
    --scenario background-noop background-yield \
    --output benchmark-results-background
```

For each background row the runner snapshots the counters before load (isolating the scenario from earlier scenarios on the same process), then after load stops it waits for in-flight work to drain up to a bound and records `started`, `completed`, `failed`, `max_inflight`, and `drain_seconds` under a `background` key. A row is marked `valid: false` (and an `[INVALID]` line is printed) unless completed tasks equal the tasks handed over (`warmup + measured`), no tasks failed, and nothing remains in flight at shutdown — so a framework cannot look faster by dropping or backlogging the work it was given. These scenarios are HTTP/1.1-only and use the built-in load generator. Client-visible throughput/latency and completed-task counts are reported separately; run Wreath on the same Uvicorn/loop configuration as Starlette, and measure `wreath-native` separately.

Scenario definitions declare framework support centrally in `benchmarks/scenarios.py`. Unsupported framework/scenario pairs are recorded as unavailable rather than failing the run, so coverage can be enabled incrementally as framework features and adapters are implemented. Litestar is deliberately excluded from the matrix: its route registration is O(n²) (it re-validates the whole routing trie on every add), so the suite's standard 10,000-route application takes ~30s to construct and cannot reliably become ready. See the note beside `ROUTE_COUNT` in `apps.py`.

## ORM against the alternatives

`benchmarks/postgres/bench_orm_competitors.py` compares Wreath's ORM with Tortoise,
SQLAlchemy, SQLModel and Peewee over the same table and rows. Every operation
returns hydrated model instances and is row-count checked before timing, and the
relationship scenarios touch each loaded relation inside the timed operation, so
no ORM can win by returning rows and deferring the join.

```bash
uv sync --group benchmark
uv run python -m benchmarks.postgres.bench_orm_competitors \
  --dsn postgresql://wreath:secret@127.0.0.1:55434/wreath \
  --output benchmark-results-orm-competitors/latest.json
```

Scenarios: `get_by_pk`, `filter_range_100`, `fetch_all_1000`, `joined_to_one`
(a to-one relation in one statement), `selectin_to_many` (children batched into a
second statement), and `join_filter_by_child` (parents filtered by a child's
column).

Two rules keep it honest. **Peewee is synchronous**: it is shown for scale but
excluded from the ranking, because it does not pay for the event loop the others
do. And **an ORM is omitted from any scenario it does not support natively**
rather than given a hand-written equivalent — Wreath has no join predicate, so it
does not appear in `join_filter_by_child`.

The other `bench_orm_*` modules answer a different question: they compare Wreath's
own paths against each other or against primitives, not against other ORMs.

## Outbound HTTP and webhooks

The focused outbound client benchmark separates fixed request serialization,
pure/native response-head parsing, and a complete managed keep-alive loopback.
The webhook microbenchmark isolates signature-base construction, exact-body HMAC
signing/verification, bounded process-local replay claims, and an A/A floor. The
dispatcher benchmark records success/retry/unknown/failure policy, backlog drain,
RSS, and end-to-end work counts. The interleaved inbound benchmark surrounds
candidate arms with equivalent A/A controls and ablates header normalization and
payload-validator compilation across the complete ASGI route. The PostgreSQL
workload measures real inbox and outbox transactions plus queue drain against an
explicit DSN. All retain raw repeated samples and fail when integrity counts
disagree.

```bash
uv run python -m benchmarks.bench_http_client \
    --output benchmark-results-http-client/latest.json
uv run python -m benchmarks.bench_webhooks \
    --output benchmark-results-webhooks/latest.json
uv run python -m benchmarks.bench_webhook_inbound \
    --output benchmark-results-webhooks/inbound-latest.json
uv run python -m benchmarks.bench_webhook_dispatcher \
    --output benchmark-results-webhooks/dispatcher-latest.json
uv run python -m benchmarks.postgres.bench_webhooks \
    --dsn "$WREATH_TEST_POSTGRES_DSN" \
    --output benchmark-results-webhooks/postgres-latest.json
```

These are development microbenchmarks, not publishable network comparisons. Use
independent peers and retain DNS/connect/TLS/pool/body timings before making an
end-to-end client claim. A small baseline is not an A/A noise study; native
parser deltas and whole-client deltas must be reported separately.

## Routing backends and routing memory

```bash
uv run python -m benchmarks.bench_routing_backends --output benchmark-results-routing-backends/latest.json
uv run python -m benchmarks.bench_routing_memory --shape app --output benchmark-results-routing-memory/app.json
uv run python -m benchmarks.bench_routing_memory --shape param-heavy --output benchmark-results-routing-memory/param-heavy.json
```

`bench_routing_backends` runs every route table implementation over the same
queries. `bench_routing_memory` measures what each backend holds resident across
an application's lifecycle — and separates what `_compile_routes()` allocates
eagerly from what appears later as groups build on first match, which is where
the decision tree keeps most of its cost on parameter-heavy tables.

## Rendering a report

`wreath-bench-report` turns saved result JSON into one self-contained local HTML
file — no network, no upload, no external fonts or scripts. It is the same
renderer that writes `latest.html` during a run, so the two never drift.

```bash
uv run wreath-bench-report benchmark-results/latest.json          # one run
uv run wreath-bench-report benchmark-results/ -o report.html --open
uv run wreath-bench-report run1.json run2.json run3.json          # medians + ranges
```

**Pass every run you have.** Given several documents it groups rows by
(scenario, framework, protocol), reports the median, and prints the observed
range beside it. That is not decoration: a scenario's run-to-run spread is
routinely larger than the gap being argued about, and the report crowns a winner
only when the leader's *worst* sample still beats the runner-up's *best* one.
Everything else is labelled `unresolved` rather than awarded to whichever median
landed higher. A single-run report has no ranges, so nothing in it can be
separated from noise — the command says so on stderr.

The existing guards still hold: no winner across mixed load generators or mixed
protocols, and never an errored row.

Results include throughput, median, p95, p99, measured and normalized batch time, errors, and environment metadata. During a run, newline-delimited progress updates are flushed about once per second. `benchmark-results/latest.json` and `benchmark-results/latest.html` are regenerated after every completed scenario, while timestamped files preserve the current run. Override all tiers with `--requests`, or tune `--warmup-requests` and `--concurrency` for exploratory checks.

## Protected sub-router pruning benchmark

The focused router benchmark builds 10,000 leaves beneath nested routers with inherited permissions and reports eligible traversal separately from an anonymous match pruned by the protected subtree's capability summary:

```bash
uv run python -m benchmarks.bench_router_pruning
uv run python -m benchmarks.bench_router_pruning --branches 50 --leaves 100 --output PATH
```

It writes one JSON document: raw compile trials (route construction excluded from
the timer), route count and shape parameters, Python/platform metadata, the
resolved `routing_implementation` (native or pure), and the raw eligible/pruned
per-match trials.

It is a framework/router microbenchmark rather than an end-to-end server comparison. Retain repeated trial output and measure full deployment behavior separately.

## Native CPU and memory-pressure benchmark

`benchmarks/bench_native_pressure.py` isolates the superlinear and unbounded
native operations addressed by `docs/plans/native-c-hotspots.md`. Each case runs
in a **fresh subprocess** so `ru_maxrss` is attributable to that case alone: the
parent spawns one child per scenario, each child prints exactly one scenario
record to stdout, and the parent writes one JSON document.

```bash
uv run python -m benchmarks.bench_native_pressure \
  --scenario all --warmup 2 --trials 9 \
  --output benchmark-results-native-pressure/before.json

uv run python -m benchmarks.bench_native_pressure \
  --scenario h2-blocked-send --warmup 2 --trials 9 --output PATH
```

`--scenario` accepts `all` or one of `h2-blocked-send`, `h2-flush-scaling`,
`h2-request-queue`, `h3-request-limit`, `router-compile`:

| Scenario | Isolates |
| --- | --- |
| `h2-blocked-send` | Whether a zero peer window bounds what an ASGI app may construct: a 64 MiB response as awaited 4 KiB chunks. Records `chunks_reached` and `bytes_written`. |
| `h2-flush-scaling` | Repeated front `memmove` when a 16 MiB then 32 MiB blocked response is released through 16 KiB WINDOW_UPDATE increments. Records each size separately. |
| `h2-request-queue` | Front deletion from the request queue: buffer then drain 25,000 and 50,000 small DATA chunks. Records consume time. |
| `h3-request-limit` | `max_body_bytes` enforcement over real HTTP/3 (needs the optional backend and an HTTP/3-capable curl; records `unavailable` otherwise). |
| `router-compile` | Decision-router compile-only time for 5,000 and 10,000 routes. Route construction is excluded from the timer. |

Each scenario record carries `python`, `platform`, `implementation`,
`executable`, `wreath_version`, `native_module`, `parameters`, `warmup_trials`,
`measured_trials`, `raw_seconds`, `median_seconds`, `p95_seconds`,
`raw_peak_rss_bytes`, `median_peak_rss_bytes`, `rss_normalization`, and
`errors`. Timing uses `time.perf_counter_ns()`. RSS comes from
`resource.getrusage(RUSAGE_SELF).ru_maxrss`, normalized to bytes: **KiB on
Linux (×1024), bytes on macOS (as-is)**; when `resource` is unimportable RSS is
recorded as unavailable rather than substituting an unrelated measure.
Scenarios that cannot run record `"status": "unavailable"` with a reason in
`errors` instead of being silently omitted.

Keep `before.json` and `after.json` as separate files and run both on the same
checkout, Python build, event loop, compiler flags, and machine load. Scaling
ratios (`scaling_ratio_*`) are the point of the paired cases; absolute times are
machine-specific. As everywhere in this suite, never call a single run a win.

## HTTP/1, routing, and storage pressure benchmark

`benchmarks/bench_native_http1_storage.py` isolates the CPU-amplification and
memory-pressure paths addressed by
`docs/plans/native-c-http1-routing-storage-pressure.md`. Same shape as the
native pressure benchmark above: one fresh child process per scenario, one JSON
document from the parent.

```bash
uv run python -m benchmarks.bench_native_http1_storage \
  --scenario all --warmup 2 --trials 9 \
  --output benchmark-results-native-http1-storage/before.json
```

| Scenario | Isolates |
| --- | --- |
| `http1-slow-head` | An unterminated 8/16 KiB request head delivered one byte at a time: does each arrival rescan the whole buffered prefix? |
| `http1-slow-chunk-line` | The same, for an unterminated chunk-size line. |
| `http1-receive-queue` | Draining 10,000/20,000 queued ASGI body messages. |
| `ws-empty-fragments` | 10,000/20,000 empty WebSocket continuation frames. Reports `retained_fragment_bytes` via tracemalloc — RSS is far too coarse to see per-fragment storage. |
| `ws-empty-messages` | 10,000/20,000 queued zero-byte messages. Reports `reading_paused`/`pause_count`: only a message-count bound can pause this. |
| `trie-adversarial-miss` | A trie miss where every level offers both a literal and a parameter branch (2**depth routes). |
| `trie-wide-fanout` | 1,000/2,000 literal children at one node, looking up the last-registered one. |
| `pg-tape-small-consume` | Consuming a 10,000/20,000-row field tape one row at a time. |
| `pg-retired-slabs` | Receive cycles against 128/256 pinned retired slabs. Reports `steps_per_cycle` from the `retired_scan_steps` counter — the bound here is a scan count, not a time. |
| `pg-bytea-text` | Decoding 10,000/20,000 hex-`bytea` text fields. |
| `multipart-peak` | Peak RSS parsing an 8/16 MiB body. Reports `copied_share_of_peak` and `addressable_share_of_peak` (field parts only — file parts must stay `bytes`). |
| `json-key-churn` | Stable versus high-cardinality (1,024 distinct) object keys. |
| `request-cookie-repeat` | 10,000/20,000 `request.cookies` reads. Reports `same_object_each_read`. |

Paired sizes exist so acceptance is expressed as **scaling** (`scaling_ratio_*`),
never a machine-specific absolute time. Records carry the same environment
metadata as the pressure benchmark plus `compiler_flags`; the RSS normalization
rule (KiB on Linux, bytes on macOS) is recorded in every record. Unavailable
scenarios record `"status": "unavailable"` rather than being omitted.

Some scenarios are deliberately not timing-based, because timing would not
answer the question: fragment retention is measured with tracemalloc, slab
reclamation with a scan counter, and the cookie cache by object identity.

## Full-lifecycle database benchmark

```bash
uv sync --group benchmark
uv run python -m benchmarks.lifecycle
```

`benchmarks/lifecycle.py` measures a complete request lifecycle against a real
database for `wreath-native`, `wreath` (uvicorn), `sanic` (native server), and
`blacksheep` (uvicorn): an authenticated admin issues a mutation on a user row.
Each of the default 100 measured requests (plus 10 warmup) performs bearer-token
authentication against a `bench_users` table (one point SELECT), an `admin` role
check, a JSON body decode, one `UPDATE .. RETURNING` on the target user, and a
JSON response.

Each framework run gets its **own fresh podman PostgreSQL container** (same
image, same `fsync=off`/`synchronous_commit=off` tuning, identical seeded rows),
torn down afterwards, so no run inherits caches or table bloat from another.
Fairness is enforced rather than assumed:

- Every application executes the same two SQL statements with one bounded
  connection pool sized to the load concurrency; both `wreath.postgres` and
  asyncpg pools use per-query acquire/release leases and prepared statements.
  Wreath apps run on Wreath's driver; Sanic and BlackSheep use asyncpg, their
  ecosystem-standard driver — the row records which (`database_driver`).
- Database pools initialize lazily on the first (warmup) request behind a lock,
  identically in every app, so lifespan support differences don't matter.
- Before measuring, the runner probes each server for a correct `200` mutation
  response payload and a `401`/`403` rejection of an invalid token. After
  measuring, it reads the table back and verifies the recorded mutation count
  equals the number of authorized requests served (`mutations_verified`), so a
  framework cannot win by dropping or short-circuiting work.

Results are written to `benchmark-results-lifecycle/` (timestamped + `latest`
JSON/HTML) in the same row format as the main suite, with added database
metadata. Requires a working rootless `podman` and the postgres image
(`--image`, default `docker.io/library/postgres:17-alpine`). Tune with
`--requests`, `--warmup-requests`, `--concurrency`, `--rows`, and `--framework`.
As with the rest of the suite, run several iterations and compare medians before
drawing conclusions; the single-machine client/server/database sharing applies
even more here.

## Which arms are in the matrix, and which are not

`FRAMEWORKS` in `scenarios.py` is the list. Three arms in it are not competitors
and should never be read as one:

| Arm | What it is | Read it against |
| --- | --- | --- |
| `blacksheep-granian` | The **same BlackSheep app** as `blacksheep`, on Granian's Rust server instead of Uvicorn | `blacksheep` — the pair isolates the server |
| `granian-rsgi` | **No framework at all**: a raw RSGI handler (`rsgi_app.py`) on that same server | Every Python arm — it is the floor their framework cost is measured *from* |
| `axum` | **Rust**, no interpreter in the request path (`rust_arms/axum_server/`) | Everything — it is the ceiling |

Without a floor and a ceiling, a matrix whose fastest and slowest rows are both
Python cannot tell you whether a spread is the framework or the runtime.

Two rules keep these honest, and both were learned by getting them wrong first:

- **Every arm gets the same CPUs and configures itself as it would in
  production.** The Rust arm was initially forced to a single thread, reasoning
  that one thread matched the single event loop each Python arm runs. Granian
  turns out to use two OS threads even at `--runtime-threads 1
  --blocking-threads 1` (counted in `/proc/<pid>/task`, not assumed), so the
  "fair" rule handed a Python server an extra core and put the Rust *ceiling*
  ~40% below it. Tokio's default worker count follows the `taskset` affinity the
  harness applies, which is the rule that actually holds every arm to the same
  cores.
- **An arm that cannot do the work is excluded from that scenario, never given a
  shortcut.** `granian-rsgi` dispatches on an exact-path dict plus one prefix
  test. That is not routing — no parameter extraction, no method resolution, no
  ordering — so it is out of the five `routing-*` scenarios (`_ROUTED_FRAMEWORKS`)
  rather than posting the best number in the table for work nobody else may skip.

### Candidates considered and not added

Mostly from the TechEmpower leaderboards. Recorded here so the same names do not
get re-litigated from scratch, and so "it is not in the matrix" never has to mean
"nobody looked at it".

The recurring blocker is the **10,000-route table**. Five of this suite's
scenarios address paths deep inside it, and a TechEmpower entry does not have to
route at all — those benchmarks define six fixed endpoints, so their fastest
entries hand-match a handful of paths. That is a legitimate way to win
TechEmpower and a poor fit here.

| Candidate | Language | Status |
| --- | --- | --- |
| `starlite` / `litestar` | Python | **Excluded, and it is the one to know about.** Starlite was renamed Litestar in 2023, so both names mean this. Route registration is O(n²): `construct_routing_trie`/`validate_node` re-walks the whole trie on every add — ~n²/2 `validate_node` calls, measured 8.0M for 4,000 routes. Building the 10,000-route table takes ~30s and flakily loses the race with the 30s readiness probe. Internal to Litestar and not fixable from here; raising the timeout would mask a boot cost the routing benchmark is partly there to compare. It is installed as a benchmark dependency, so re-testing it after a Litestar release is a one-line change. |
| `granian [rsgi]` | Python | **Added** as `granian-rsgi`. |
| `fastwsgi` | Python (C server) | Not added. It is a WSGI server, not ASGI — the TechEmpower "[asgi]" tag is wrong. It could host the existing Flask app as a `flask-fastwsgi` pair, exactly like `blacksheep-granian`, and that is a cheap and worthwhile addition; it just is not done yet. |
| `panther` | Python | **Added.** A genuine peer arm — real router, builds the 10,000-route table in 0.30s. It takes every scenario except streaming, background tasks, WebSockets and `validated-body`. One trap worth knowing: Panther injects the request by filtering `func.__annotations__` with `v in {BaseRequest, Request, bool, int}`, an identity check against the real classes. `apps.py` has `from __future__ import annotations`, so annotations are strings, no annotation ever matches, and the handler is called without its `request` — a 500 on every request-reading endpoint, each logging a full traceback. The class is bound onto `__annotations__` before `API()` reads it; writing `request: Request` in the signature cannot work in that module however it is spelled. |
| `ntex` (+ `sailfish`) | Rust | Not added. Has a real router, so the route table is not a blocker — this is the strongest remaining candidate. `sailfish` is a *template engine*, not a framework, and would only matter if the Rust arms took part in the `template` scenario, which they do not. |
| `xitca-web` | Rust | Not added. Same position as `ntex`: a real router, a straightforward port of `rust_arms/axum_server/src/main.rs`. |
| `may-minihttp` | Rust | Not added, and needs a decision first. It has **no router** — you match the path by hand — so it would land where `granian-rsgi` is: a floor arm excluded from the routing scenarios, not a framework. Worth having as the absolute ceiling, but it should be labelled as one. |
| `h2o` | C | Not added. h2o is a server; the TechEmpower entry is a bespoke C handler built against `libh2o`, which is not packaged here and would have to be built from source as part of the benchmark build. Large cost for a ceiling `axum` already establishes. |
| `lithium-postgres` | C++ | Not added. Needs the Lithium framework and its C++ toolchain, and the entry is database-shaped — it belongs with the PostgreSQL battery rather than the framework matrix, where it would have no counterpart to be compared against. |

Adding a Python arm means a branch in `apps.py`, an entry in `FRAMEWORKS`, the
capability sets it belongs to, and a `REQUEST_TIERS` entry. A compiled arm also
needs a crate under `rust_arms/` (a member of that workspace, so it shares the
one `target/`) and a `_server_command` branch in `run.py`;
build it **before** quieting the machine, never during a run — `tools/bench-quiet.sh`
does that.

## The generator has to be faster than what it measures

`h2load` ran with **one thread** for the whole life of this harness, and one
h2load thread saturates near 130k req/s on a six-core desktop. Every arm faster
than that was reporting the generator's ceiling as its own throughput, and the
reading did not move however much faster the server got. Against one unchanged
two-worker metal server:

```
h2load -t 1: 133,423 req/s
h2load -t 2: 174,874 req/s
h2load -t 4: 222,064 req/s
```

That also made a multi-worker measurement impossible: server workers 1, 2 and 4
all read ~118k req/s, because the generator was the bottleneck in all three.
With one thread per generator core the same sweep reads 121k / 196k / 225k, and
p99 drops from 0.457 ms to 0.194 ms.

The default is now one h2load thread per physical core the generator process
actually has, capped by the connection count; `--generator-threads N` overrides
it, and `generator_threads` is recorded in the result metadata. **Throughput
rows recorded before this are suspect above ~130k req/s** — treat them as a
lower bound on the server and a measurement of h2load.

A generator can still be the ceiling if it has too few cores: a plateau that
does not move when the server gets more workers is the signature, and the
metadata is there so you can check what drove the run before believing it.

## Workers: one column or the other, never a mixture (`--multi`)

Every arm is given exactly the same number of workers, and the harness pins the
whole tree to that many physical cores. There are two columns and they are not
comparable to each other:

```console
uv run wreath-bench                 # one worker, one core, every arm
uv run wreath-bench --multi         # half the physical cores; one worker each
uv run wreath-bench --multi 4       # four server cores, four workers per arm
uv run python -m benchmarks.run --workers 4     # the matrix on its own
```

`--multi` on its own gives the server a *third* of the physical cores, so the
generator keeps two cores for every one the server gets. One h2load thread
saturates near 133k req/s and one metal worker serves near 120k -- they cost
about the same per core -- so an even split would stand the generator up at
parity with the thing it is measuring, and a plateau would read as the server's
ceiling when it is really the client's. `--multi N --workers M` is allowed and
means what it says: N server cores, M workers.

On a six-core machine that is `--multi` -> 2 workers with 4 generator cores.
Asking for more (`--multi 3` -> 3 and 3) puts you back at parity: still worth
running, but read it as a lower bound and check `generator_threads` in the
metadata before believing a plateau.

**This exists because two arms used to size themselves.** Tokio derives its
worker count from the CPU affinity unless told otherwise, so on one SMT core the
axum arm quietly ran two worker threads against every other arm's one; Granian
defaults to more than one worker. Both are now pinned in both directions, at one
worker as much as at eight — `--threads 1` gives axum a genuine current-thread
runtime (one OS thread, verifiable in `/proc/<pid>/task`). Rows recorded before
that are not like-for-like at the single-worker end.

How each arm expresses a worker:

| arm | mechanism |
| --- | --- |
| `wreath`, `starlette`, `fastapi`, … | uvicorn's supervisor: one shared socket across worker processes |
| `wreath-native`, `wreath-metal` | one process per worker on an `SO_REUSEPORT` listener group, each pinned to its own core |
| `granian-rsgi`, `blacksheep-granian` | Granian workers, one runtime thread each |
| `sanic` | Sanic's process manager |
| `axum` | Tokio worker threads (`--threads`), not processes |

The `server` column in the report records the worker count and the CPU set, and
`workers_per_arm` is in the result metadata, so two runs cannot be compared by
accident.

## Interpretation rules

- The bundled client and server share a machine and event loop resources. It is a fast feedback tool, not independent proof.
- Flask is adapted from WSGI to ASGI, so its result includes adapter overhead.
- These endpoints intentionally avoid validation. Later suites will separately measure validated input, middleware, injection, streaming, WebSockets, and full-stack behavior.
- Run multiple iterations, randomize framework order, pin CPUs, disable frequency scaling, and retain raw data before publishing conclusions.
- Compare framework overhead on a common server. Compare native/default deployment stacks in a separate report.
- Never compare differently shaped or differently validated responses under one scenario name.

## Protocol dimension (HTTP/1.1, HTTP/2, HTTP/3)

Protocol is an **orthogonal result dimension**, not a framework. Each `Scenario`
declares `protocols` independently of `frameworks`; `ws-echo` is HTTP/1.1-only.
Every result row carries `protocol`, `transport`, `secure`, `alpn`, `connections`,
`max_streams_per_connection`, `trial`, `load_generator`, `load_generator_version`,
and `server_tls_version`, and the request count is completed request/response
streams (never frames or packets).

The bundled generator is HTTP/1.1-over-cleartext only and is labeled
`load_generator=builtin`. HTTP/2 and HTTP/3 measurement requires an independent,
protocol-capable generator (an HTTP/3-enabled `h2load`) run as a subprocess
(`--load-generator h2load`, `--protocol h2 h3`, `--tls-cert/--tls-key`); a client
protocol library is never imported into the runner. The report **suppresses any
winner** across mixed generators or across protocols and prints a warning, so
protocol rows are compared, not ranked. Errored rows are never crowned.
`benchmarks/wreath_server.py` accepts `--protocol` and `--tls-cert/--tls-key` and
starts exactly the requested protocol set.

## HPACK decode microbenchmark

```bash
uv run python -m benchmarks.bench_hpack_decode \
  --warmup 2 --trials 9 \
  --output benchmark-results-hpack/after.json
```

This feeds legal Huffman-coded request blocks through the native HTTP/2 protocol
for common, cookie-sized, 1 KiB, 16 KiB, and mixed-code values, while checking a
malformed block separately. The timed region includes frame and stream dispatch;
retain raw trials and an interleaved A/A run before attributing a decoder delta.
Never overwrite the untouched baseline.

## Browser-policy and compression microbenchmark

```bash
uv run python -m benchmarks.bench_web_policy_compression \
  --warmup 2 --trials 9 \
  --output benchmark-results-web-policy/after.json
```

This compares policy parsing, adaptive missing-header insertion
at 64–512 existing headers (including duplicate additions), and stdlib/native-zlib
gzip paths across compressible and incompressible payloads. Results retain every
trial, compression ratio, Python/platform data, and linked zlib metadata. Policy and
compression results are not interchangeable: compression throughput must be
reported together with output size.

Profile one middleware path without import noise using, for example:

```bash
uv run python benchmarks/profile_web_policy.py csrf 30000
```

Measure valid unsafe-request CSRF validation without profiler overhead using repeated
raw trials:

```bash
uv run python benchmarks/bench_csrf.py --warmup 2000 --iterations 20000 --trials 9
```

The retained profile comparison is `benchmark-results-web-policy/profile-summary.md`.

## Planned benchmark tiers

1. Direct ASGI-call microbenchmarks for routing and response emission
2. Common-server HTTP/1.1 framework comparisons
3. Native recommended deployment comparisons
4. Body parsing, validation, middleware, dependency injection, and errors
5. Streaming, WebSockets, keep-alive, slow clients, and backpressure
6. Multi-process, free-threaded, optional-JIT, and subinterpreter experiments
7. Memory, allocations, startup, import time, and long-running stability

## Neutral workload suite

`benchmarks/workloads/` is a framework-neutral application exercising the seven
web-workload shapes through public Wreath APIs only — no third-party benchmark's
routes, table names, constants, or randomization. Domain names are generic
(`Widget`, `Quotation`).

```bash
uv run python -m benchmarks.workloads.verify        # assert semantic properties
uv run python -m benchmarks.workloads.bench         # per-shape latency
```

The verifier asserts observable *properties* — one database operation per point
read, one `Sync` per fan-out input, read-before-write ordering, unique
application-generated update values, template escaping and UTF-8 output, header
and framing correctness, query-limit behaviour, and snapshot misses — using an
in-process PostgreSQL wire stand-in so it runs without a live server. A future
external adapter can map these primitives to prescribed conformance names
without changing `src/wreath`.
