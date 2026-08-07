# Where the native path stands, 6 August 2026

Status: **measurement only.** No code changed. Run on this laptop — an AMD Ryzen
7 7730U, 8 cores / 16 threads, 15 W — so every number here describes *this*
machine and nothing else.

Two things were run: a few of the original framework scenarios on the
`wreath-native` arm, and the parked TechEmpower recreation in `.futures/`, which
had never been run before.

## The headline

On a realistic database workload — twelve ORM rows, one ephemeral row added per
request, sorted in the application, escaped HTML, against a real PostgreSQL 17 —
**the native stack serves 65,663 requests a second and its pure-Python twin
serves 12,917.** The accelerators are worth **5.1x**, and that is the whole
accelerator stack measured in one number for the first time.

## The first thing I have to say is a correction

There are recorded framework results in `benchmark-results/` from 28 July. Read
naively, today's numbers look like a 2x regression against them.

They are not comparable, and I nearly reported that they were.

| | 28 July record | today |
| --- | --- | --- |
| platform | Debian 13, kernel 6.12 | CachyOS, kernel 7.1 |
| Python | 3.14.6, **Clang**-built | 3.14.6, **GCC**-built |
| concurrency | 8 | 4 (until I matched it) |
| requests | 4,000 | 1,000 (until I matched it) |

**Different machine, different compiler, different load settings.** Matching the
load settings closed none of the gap, which is what said the rest was the box.
Nothing in `benchmark-results/` is a baseline for this laptop, so "where we
stand" can only mean "where we stand here, today".

## Original scenarios, `wreath-native`

Three trials each, concurrency 8, 4,000 requests, zero errors throughout.

| scenario | unpinned | **pinned** | pinning gains |
| --- | --- | --- | --- |
| plaintext | 68,229 | **84,193** | +23% |
| json | 65,379 | **84,536** | +29% |
| parameter | 65,568 | **79,130** | +21% |
| middleware-noop | 63,676 | **76,396** | +20% |
| validated-body | 29,168 | **37,076** | +27% |
| auth-jwt | — | **27,063** | — |

**Pin the benchmark or do not believe it.** `wreath-bench` pins to physical
cores; `python -m benchmarks.run` does not, and the difference is a fifth to
nearly a third of the result — larger than any code change made this week. Every
figure quoted below the line here is the pinned one.

The shape is what you would expect: a plain response and a JSON response cost
about the same (the encoder is native and quick), a bound parameter costs a
little more, body validation costs half the throughput, and a JWT-authenticated
request costs a third of it again.

## The TechEmpower recreation, Wreath arms only

Fortunes board, 5 seconds per level, one trial, 2 database cores / 3 server
cores / 3 generator cores. Every arm verified its output and returned zero
socket errors and zero non-2xx responses.

| arm | c=16 | c=64 | c=256 | c=512 | best | vs native |
| --- | --- | --- | --- | --- | --- | --- |
| **wreath** (metal, 6 workers) | 41,738 | 54,490 | 62,918 | **65,663** | **65,663** | 1.00x |
| wreath [floor] | 42,091 | 54,252 | 63,090 | 63,149 | 63,149 | 0.96x |
| wreath [micro] | 37,416 | 49,812 | 56,414 | 57,428 | 57,428 | 0.87x |
| wreath [asyncio] | 33,004 | 40,359 | 46,687 | 49,608 | 49,608 | 0.76x |
| wreath [flight] | 27,985 | 33,102 | 35,680 | 36,297 | 36,297 | 0.55x |
| wreath [1 worker] | 22,276 | 29,543 | 29,526 | 29,504 | 29,543 | 0.45x |
| **wreath [pure]** | 11,256 | 12,254 | 12,824 | **12,917** | **12,917** | **0.20x** |

### What each gap buys

**The accelerators are worth 5.1x.** `WREATH_PURE=1` replaces the C accelerator,
the native HTTP protocol and the native PostgreSQL decoder with their Python
twins, keeping the reactor. 65,663 against 12,917. That is the clearest
justification for the native path in the tree, and it had never been measured
end to end before today.

**The native reactor is worth 1.32x over asyncio** — 65,663 against 49,608 —
and rather more than that in the tail (below).

**Running one worker instead of six costs 55%.** Not a sixth: with three server
cores, six workers on SMT siblings recover about 2.2x, which is the honest
scaling for this shape.

**Detailed telemetry costs 45%.** The `flight` arm is the entry with
`--telemetry detailed`, and it drops to 36,297. That is a real number to put in
front of anyone about to turn Detailed on in production.

**The full stack costs 4% over the floor.** `floor` reaches 63,149 against the
entry's 65,663 — so routing, binding, the middleware tape and the template
engine together cost about 4% on a request whose work is dominated by the
database. That is the most reassuring number here.

### Latency

Throughput at c=512 is saturation; the tail there is not a service level. At
c=64, which is a working level:

| arm | rps | p50 | p99 | p99 at c=512 |
| --- | --- | --- | --- | --- |
| wreath | 54,490 | 1.00 ms | 2.42 ms | 62 ms |
| wreath [floor] | 54,252 | 1.04 ms | 2.05 ms | 66 ms |
| wreath [micro] | 49,812 | 1.14 ms | 2.20 ms | 68 ms |
| wreath [asyncio] | 40,359 | 1.50 ms | 2.32 ms | **1,620 ms** |
| wreath [flight] | 33,102 | 1.31 ms | 9.53 ms | 122 ms |
| wreath [pure] | 12,254 | 4.61 ms | 10.29 ms | 191 ms |

The asyncio row is the one to look at twice. At a working level its tail is
fine — 2.32 ms, no worse than the native reactor. Under saturation it collapses
to **1.62 seconds at the 99th percentile**, twenty-six times the native
reactor's 62 ms. The reactor's value is not really the 32% of throughput; it is
that the thing degrades gracefully when it is overloaded.

## Three things wrong with the rigs, found by running them

- **`wreath-bench` refuses to run because of its own invoking shell.** It
  detected "1 competing workload — another benchmark" and named the `zsh` that
  was launching it, because the process command line contains the word. The
  refusal is right to exist and this instance of it is a false positive;
  `--allow-competing` is the documented escape and these results used it.
- **The `.futures` uvloop arm is blocked by a stale note, not a probe.** Its
  `blocked=` field is a hard-coded string saying uvloop is not installed. It is
  installed (0.22.1, pulled in by `wreath-bench`). Deleting that one line would
  fill the third point on the reactor axis. I have not touched it, because
  `.futures/` is a deliberately parked draft and editing it is your call.
- **`benchmarks/postgres/bench_orm_hydrate.py` assumed the `public` schema**,
  which fails on the container AGENTS.md tells you to run. Fixed earlier this
  session.

## Caveats, plainly

- One trial on the TechEmpower board, so **no interval** — the harness's own
  README is emphatic that a row without a range has not separated itself from
  the row above it. The gaps discussed above are 1.3x and larger, which single
  trials can carry; the 4% floor-versus-entry gap is *not* one of them and
  should not be quoted without repeats.
- 5 seconds per level against Round 23's 15.
- A 15 W laptop part under sustained load throttles. Absolute numbers here will
  not match a desktop or a server, and are not meant to.
- The machine could not be quieted to tier 1 (`wreath-bench-quiet --tier 1`
  needs a sudo password), so the governor was free to move. Instruction-count
  measurements elsewhere in this session were used precisely because they do not
  care; throughput numbers do.
