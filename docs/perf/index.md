---
description: How fast Wreath is, how that's measured, and where the speed comes from — a reproducible benchmark run and the engineering behind it.
---

# Performance

Wreath is built for the request → metal → response hot path to be short and
predictable. This page is the honest version of "is it fast?": medians across
three benchmark passes, the methodology that makes them meaningful, and — for the
people who scrolled here on purpose — the mechanics that produce the numbers.

Three wreath arms appear on purpose, and each gets its own colour so it never
blends into the field:

- **Wreath (metal)** — wreath's **native C server** driving its own io_uring
  event loop (`--loop metal`). The headline.
- **Wreath (native)** — the same **native C server**, on the uvloop event loop.
- **Wreath (ASGI)** — wreath as a plain **ASGI app** under uvicorn, with its own
  server not in the picture. The baseline that shows what the C server buys.

So the three bars isolate one variable at a time: ASGI → native is *the C server*,
native → metal is *the io_uring loop*. Competitor bars are a muted hatch — "the
field" at a glance.

!!! note "Medians, on one machine"
    Every bar is the **median of three passes** on one workstation, with the
    run-to-run range kept in the underlying data. Wreath's contribution rules
    forbid a performance claim from a single run — a single run once smuggled in
    an "optimization" that evaporated on a second look — so read these as *this
    machine, this workload*, and re-run them yourself (recipe at the bottom). The
    charts are minted at build time from the JSON by
    [wreath's own SSG](../guides/docs-ssg.md), so they can't drift from the data.

## Throughput: serving HTTP

Requests per second, higher is better. Start with **plaintext** — a few constant
bytes, no work — which is the scenario *most* flattering to a competitor, because
you're mostly measuring the socket and the loop, not the framework:

```chart
source: perf/data/throughput.json
data: results
x: framework
y: requests_per_second
where: scenario=plaintext
title: Plaintext response (requests/sec, higher is better)
sort: desc
```

Even here — the case with the least room to differentiate — **Wreath (metal) is
roughly 2× the fastest competitor**, and the native arm close behind. What's
contributing to the win: on `--loop metal` there is no asyncio underneath. The
socket read lands straight in a kernel-registered buffer (zero copy), submission
and completion share a single `io_uring_enter`, and the adaptive busy-poll keeps
the CPU on the socket through a burst instead of parking and being woken. The
**Wreath (ASGI)** bar — the same app on uvicorn — sits right next to
BlackSheep, which is the tell: at the bottom wreath is an ordinary well-built ASGI
framework, and almost the whole jump to the top two bars is wreath's own **native
C server** replacing uvicorn.

On a **JSON body round-trip** — decode a request body, serialize a response — the
per-request overhead of a validation-heavy stack starts to bite:

```chart
source: perf/data/throughput.json
data: results
x: framework
y: requests_per_second
where: scenario=json-body
title: JSON request body → JSON response (requests/sec)
sort: desc
```

What's contributing to the win: the body never crosses the Python boundary as a
throwaway `bytes` per chunk — the stream-fusion C API reads it in place — and
wreath's JSON path is C on the way in and out. The work that a Pydantic-style
model does per field simply isn't on the hot path unless you put it there.

And on work most micro-frameworks push to a plugin — **auth + RBAC on every
request** — the field thins out, because the others aren't carrying it in-process:

```chart
source: perf/data/throughput.json
data: results
x: framework
y: requests_per_second
where: scenario=auth-rbac-allow
title: Authenticated + RBAC-authorized request (requests/sec)
sort: desc
```

What's contributing to the win: the single-pass pipeline classifies the route
**once** and hands authorization an opaque ticket to resolve against the caller's
capability mask — no second walk of the route tree — and the Cedar policy check
underneath is the C evaluator benchmarked [below](#cedar-authorization). A blank
bar isn't a zero: it means the framework has no in-box equivalent to run.

## Latency, not just throughput

Throughput hides the tail. Median is easy; it's p95 and p99 — the requests users
actually complain about — that separate a tight loop from a jittery one. Plaintext,
this run:

| Framework | p95 (ms) | p99 (ms) | req/s |
| --- | --- | --- | --- |
| **Wreath (metal)** | **0.082** | **0.103** | 111,665 |
| Wreath (native) | 0.098 | 0.107 | 101,241 |
| Wreath (ASGI) | 0.176 | 0.187 | 55,192 |
| BlackSheep | 0.177 | 0.185 | 54,631 |

The metal arm is fastest at the tail *and* the median — the hashed timing wheel
and the `Handle`-free task-step aren't chasing a higher peak, they're flattening
the distribution, so p99 doesn't blow out under load.

## Beyond HTTP: the batteries are fast too

The framework matrix is only half the story — wreath ships an ORM, migrations,
and a policy engine, and those are measured with the same discipline.

### Migration resolution

Resolving a migration graph to the current head, 16 revisions, medians over nine
trials — against the tools wreath is meant to replace:

```chart
source: perf/data/migration.json
data: results
x: tool
y: resolutions_per_second
title: Migration graph resolution (resolutions/sec, higher is better)
sort: desc
```

What's contributing to the win: wreath resolves the revision graph in native
code with the history laid out for linear traversal — **~21× Alembic** and ~7×
Django on this run. Alembic's cost is re-parsing and re-walking Python revision
modules on every resolve; wreath does the walk once, in C, over a compact
structure.

### Cedar authorization

Evaluating a six-policy set (one allow + one deny per call), wreath's built-in
Cedar engine against `cedarpy`, the Rust binding to the reference
implementation:

```chart
source: perf/data/cedar.json
data: results
x: engine
y: calls_per_second
where: phase=evaluate
title: Cedar policy evaluation (authorizations/sec, higher is better)
sort: desc
```

What's contributing to the win: the engine compiles the policy set once, in
Python at startup, and the per-request evaluator walks that flat tape against
pre-interned entities in C — **~820k authorizations/second**. That headroom is
why putting authorization on *every* request (the RBAC bar above) costs so
little.

## How it's measured

A benchmark is easy to lie with and easy to be fooled by, so wreath's suite is
built to make honesty the path of least resistance:

- **Medians with ranges.** The canonical run does three passes; every number here
  is the median across them, and the underlying JSON keeps each pass's value so a
  range is available — a scenario's run-to-run variation is routinely larger than
  the gap being argued about.
- **A winner only when it's real.** The suite crowns a leader only when its
  *worst* sample still beats the runner-up's *best* — no coin-flip podiums. A
  single pass can never crown anyone; that's the discipline a raw one-shot number
  quietly skips.
- **Work is verified.** It counts completed background tasks, checks row counts,
  and reads mutations back from the database, so no framework can look fast by
  quietly doing less.
- **Honest about its own limits.** The bundled load generator shares a machine
  with the server — a fast tool for sensing *direction*, not independently
  generated proof. Where a comparison isn't quite like-for-like (a different
  template engine, an ORM missing a feature, a WSGI adapter in the path), the
  report says so instead of letting a green cell imply more than it should.

The HTTP arms — **Wreath (metal)**, **Wreath (native)**, and **Wreath (ASGI)** —
are described at the top; the migration and Cedar sections compare wreath
against the tool it replaces (Alembic/Django, `cedarpy`).

This run:

| | |
| --- | --- |
| Python | 3.14.6 |
| Platform | Linux, x86-64, process tree pinned to the P-cores |
| Passes | 3 (medians reported; 100-request warm-up per scenario, discarded) |
| HTTP arms | uvicorn (ASGI), uvloop (native), io_uring (metal) |
| ORM / migration / Cedar | 9 trials each, medians |

## Where the speed comes from

The numbers above are an outcome, not a technique. Here's the machinery — grouped
by where it sits on the request → metal → response path. Almost all of it is in
the [native server](../guides/server.md), which a deployment behind somebody
else's ASGI server does not get — the framework works there, the server bars do
not apply.

### The metal event loop (ingress)

The fastest arm, **Wreath (native)**, doesn't run on asyncio at all when you ask
for `--loop metal`: each worker drives its own **io_uring** loop that owns the
HTTP/1 sockets directly.

- **Kernel-owned sockets, zero-copy reads.** Multishot accept and multishot
  receive over 1024 registered 16-KiB buffers; the parser reads straight out of
  the kernel-provided buffer. Each buffer carries a generational token, so a
  stale kernel buffer-ID is rejected in O(1) before protocol code ever touches
  the memory.
- **One syscall does submit *and* wait.** Async submission-queue entries are
  batched in userspace and, with `IORING_SETUP_DEFER_TASKRUN`, published in the
  *same* `io_uring_enter` that blocks for their completions. A quiet-to-busy
  transition doesn't pay a syscall per operation.
- **An adaptive busy-poll that learns your traffic.** After an empty completion
  probe the loop keeps an EWMA of empty-CQ-to-arrival gaps and their deviation.
  It spins — a zero-timeout `io_uring_enter`, GIL released, no sleep/wakeup IPI —
  *only* when it predicts the next packet is under 100 µs away, for a budget of
  `ewma + 2σ` clamped to [2 µs, 50 µs]. It adapts fast toward load onset and slow
  toward idle, so a wrong guess costs one bounded spin, not standing latency.

!!! note "The timer that wouldn't settle"
    A server churns **two** timers per request — keep-alive and request deadline —
    that are almost always cancelled before they fire. asyncio's binary-heap
    timers are O(log n) with a cancellation-compaction pass; wreath uses a
    **hashed timing wheel**: O(1) insert and O(1) cancel by splicing an intrusive
    linked-list node, no reallocation, no heapify. A segment tree over slot minima
    finds the next deadline, and `io_uring_enter(EXT_ARG)` blocks straight off the
    wheel. The whole story is written up in
    [The timer that wouldn't settle](../explorations/the-timer-that-wouldnt-settle.md).

- **Skipping asyncio's `Handle` frame.** For CPython 3.14's exact
  `TaskStepMethWrapper` zero-arg callback shape, the loop enters the captured
  `Context` and calls the C task-step directly, skipping the Python `Handle._run`
  frame that asyncio wraps around every coroutine resume. `call_soon` likewise
  enqueues a freelisted C ready-handle onto a shared deque instead of building a
  Python `Handle` — while preserving asyncio's per-turn fairness quantum (the
  ready queue is drained by a snapshot count, not to exhaustion).

### The request pipeline (routing → auth → dispatch)

- **Classify once, resolve later.** A request walks the route tree exactly once
  and gets back an opaque compiled *ticket*; authorization then resolves against
  the caller's capability mask **without re-walking** method/path nodes. Public
  and missing routes never authenticate — even when credentials are present — and
  protected routes authenticate at most once. In a focused 385-route measurement,
  a protected-allow dispatch went ~3,363 ns → 576 ns and protected-deny
  ~5,535 ns → 567 ns. (End-to-end throughput from that change was mixed and noisy;
  no blanket win is claimed — see the guardrail below.)
- **Policy routing with a discriminating-byte key.** Inside a
  `(method, segment-count)` group, matching becomes
  a per-position bitmask AND walked strongest-discriminator-first with early exit,
  and authorization is one more AND. That erases the decision tree's super-linear
  parameter-folding: a 512-route/50%-param table drops from ~20 MB resident to
  ~264 KB (**76×**), at −14% to −37% instructions per match. A compile-time key
  packs the bytes that actually separate literals into one integer, removing the
  hash + `memcmp` for 85–90% of lookups. This is the sole native routing table;
  the experimental configuration aliases and their implementations are gone.
- **A header index the whole middleware stack shares.** The C parser already holds
  every header; proxy / CSRF / request-ID / auth each want *different* ones, so
  the index is shared and updated in place rather than rebuilt per lookup. CORS
  was rewritten to read `request.method` and the C `webpolicy` header finder
  instead of materializing the full ASGI scope dict — eleven fewer Python frames
  per request.

### Protocol and egress

- **Stream-fusion C API.** The zero-object ingress seam is generalized into a
  capsule any native protocol registers; the metal transport picks the first
  match and socket reads bypass the Python `BufferedProtocol` calling convention
  entirely — no per-chunk `PyLong`/memoryview allocation. Postgres and the
  outbound HTTP client are the second and third implementers, so the fast path is
  shared, not bespoke.
- **Incremental delimiter scanning.** Head, chunk-size, and trailer parsing use
  state-relative cursors and a `find_sub_from()` helper, so byte-at-a-time
  delivery is linear, not quadratic — a 16-KiB head fed one byte at a time stays
  under 2.5× the whole-buffer median instead of blowing up.
- **Table-driven HPACK Huffman decode.** One table lookup per compressed byte
  (emitting ≤2 symbols) replaces up to eight dependent bit-tree transitions, with
  precomputed EOS/padding validation — the code table stays the single source of
  truth, the transition table is generated from it.
- **HTTP/2 fairness by Deficit Round Robin + RFC 9218.** When streams share
  renewed flow-control credit, the native server round-robins with persistent
  deficits instead of draining the first stream, and layers RFC 9218 urgency
  (including `PRIORITY_UPDATE`) on top — so one large response can't monopolize
  the loop and a high-urgency request can preempt.

!!! warning "What we *don't* claim"
    Speed pages usually list only the wins. Several intuitively-appealing ideas
    here were **built and then rejected on measurement**: GIL-release around
    WebSocket masking (reattachment latency under contention), a native zlib
    rewrite, GIL-release for HPACK (the 16-KiB worst case is only ~31 µs
    end-to-end — too short to detach), and Swiss-table control bytes for the
    router. They aren't in the numbers because they didn't earn it.

!!! tip "Two kinds of evidence, kept apart"
    Latency and throughput are noisy — wreath's box has an ≈0.8% A/A floor, and a
    change under it is reported as *no result*, not a win. So structural wins are
    measured **structurally**: [`wreath-request-trace`](../guides/testing.md)
    counts exact Python↔C boundary crossings per request, `wreath-complexity-probe`
    reads an operation's growth exponent off a log-log slope (catching a
    reintroduced quadratic that a fast machine would hide), and the metal tier has
    gates on fixed native RSS, SQE counts, and `io_uring_enter` calls per request.
    A crossing-count win and a stopwatch win are never quoted as the same thing.

## Reproduce it yourself

The whole point of the honesty machinery above is that you don't have to trust
the chart — you can regenerate it:

```bash
uv run wreath-bench                    # full battery: pinned, 3 passes, db + lifecycle
uv run wreath-bench --matrix-only      # just the framework matrix (this page's data)
uv run wreath-bench --no-db --passes 1 # matrix + in-process webhooks, one pass, no podman
uv run wreath-bench --pin none         # don't pin to the P-cores
```

Each run drops timestamped JSON and an HTML report under `benchmark-results*/`;
`wreath-bench` combines the matrix passes, migration, Cedar, ORM, and lifecycle
into one `full-battery.html` using **exactly** the aggregation the charts here
reproduce — group by (scenario, framework), take the median across passes, keep
the range. Point a `chart` block at your own JSON and you have this page for your
machine — the [docs-site guide](../guides/docs-ssg.md) shows the block that does it.

!!! tip "Comparing your own change"
    Ablation runs live in their own directories (`benchmark-results-metal-*`,
    `benchmark-diagnosis-*`) so a before/after never overwrites your baseline.
    That is the discipline the contribution rules require: an optimization earns
    its place by showing up as a range that clears the noise, not a single
    faster number.

## The raw data

The charts read three committed snapshots the build publishes alongside them:
[`throughput.json`](data/throughput.json) (HTTP matrix — median of three passes,
with per-pass min/max),
[`migration.json`](data/migration.json), and
[`cedar.json`](data/cedar.json). Framework names are relabelled to the current
`Wreath (metal|native|ASGI)` arms; every number is a median straight from the
benchmark output. Nothing here is hand-typed — if a snapshot changes,
`wreath docs build` re-mints every chart from it.
