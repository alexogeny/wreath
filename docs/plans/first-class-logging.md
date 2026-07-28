# First-class logging

Status: stages 1–6 landed and wired into the server, the native emitter landed,
and the six measurements the plan owed have been run. Stage 7 deferred by
decision.
See [ADR 0025](../decisions/0025-a-log-record-is-a-ring-cell.md) for why logging
is a ring cell rather than a subsystem, and
[the guide](../guides/logging.md) for the user-facing story.

## Shape

```
call site (import)        registry: template, severity, types, dispositions
      |
handler (request path)    level check -> limiter -> pack into a 64-byte cell (C)
      |                                        \-> or buffer, if TRACE/DEBUG in a request
      |                                        \-> or stage, if not on the loop
      v
projector thread          drain, join to trace by request_id, settle on a quiet cycle
      |
      +---> writer thread (bounded queue)   render -> text / JSON lines
      +---> export pipeline (bounded queue) -> build_logs_request -> OTLP /v1/logs
```

## Stages

| # | Stage | State |
| --- | --- | --- |
| 1 | `EventKind.LOG` cell, typed argument packing, severity, loss reasons, C mirror | landed |
| 2 | Interned call-site table; `log.event` tier and `log.info` tier | landed |
| 3 | Projector integration, writer thread, text and JSON renderers | landed |
| 4 | Failure-triggered promotion, per-call-site limiting | landed |
| 5 | Canonical log line (`scope.set` / `log.set_field`), `MemoryBudget.logging` | landed |
| 6 | `build_logs_request`, third export tick, stdlib bridge, doctor check | landed |
| — | Server wiring: per-request scope, recorder-backed sink, writer lifecycle | landed |
| — | HTTP/2, HTTP/3 and WebSocket scopes; full `LoggingConfig`; `request.event` | landed |
| — | The six measurements | landed |
| — | Native emitter `wreath_nfr_log`, with a byte-for-byte parity corpus | landed |
| — | Off-loop emission: bounded stage, loop drain, `LOG_FLAG_OFF_LOOP` | landed |
| 7 | mmap-backed forensic ring, binary archival stream, decoder | deferred |

Stage 7 is deferred but **not designed out**: the cell carries its schema version
in byte 0 and every decode validates lengths against the buffer, so a file-backed
ring can be added without a format break. That framing was a constraint on the
stage 1 commit, not a later task.

## Files

| Concern | File |
| --- | --- |
| Cell layout, severity, argument packing | `src/wreath/_flight_schema.py` |
| C mirror and static asserts | `src/wreath/_native/flight_schema.h` |
| Interned sites, packing, rendering, spec blobs | `src/wreath/_logsite.py` |
| Per-request buffering, per-site limiting, off-loop staging | `src/wreath/_logscratch.py` |
| Renderers, writer thread, canonical line | `src/wreath/_logsink.py` |
| Public API | `src/wreath/logging.py` |
| Ring drain and trace join | `src/wreath/_projector.py` |
| OTLP logs mapping | `src/wreath/_otlp.py` |
| Export tick | `src/wreath/_export.py` |
| Fixed-size budgets | `src/wreath/telemetry.py` (`LoggingConfig`) |
| Split-stream check | `src/wreath/doctor.py` |
| Native emitter | `Recorder.log` (`_flightmodule.c`), `wreath_nfr_fingerprint` (`flight.c`) |
| Ring publish seam | `wreath_nfr_publish_cell` (`flight.c`), `Recorder.publish_log` |
| Per-request scope | `src/wreath/app.py` (`_handle_http` / `_finish_http`) |
| Runtime, writer and off-loop lifecycle | `src/wreath/server.py` (`_create_logging`, `_drain_off_loop_logs`) |
| Measurements | `benchmarks/bench_logging.py`, `benchmarks/results/logging_2026-07-28_*.json` |

## What the measurements said

Run on CPython 3.14, Linux x86-64, 2026-07-28.
`uv run python -m benchmarks.bench_logging --suite all`. Arms interleaved, an A/A
control at the far end of each round, medians; a delta below twice the measured
floor is reported unresolved rather than as a number. Two retained runs:
`benchmarks/results/logging_2026-07-28_baseline.json` (the Python emitter) and
`logging_2026-07-28_native.json` (after). Neither is a single run — each arm is
11 rounds of 20,000 iterations — but both are one machine, and a second machine
would be worth having before any of this is quoted anywhere permanent.

**1. `SITE(a, b)` against the alternatives.** Per record, two arguments:

| | before | after |
| --- | --- | --- |
| `SITE(user, resource)`, string raw | 2.55 µs | **0.42 µs** |
| `SITE(user, resource)`, string hashed *(the default)* | 9.17 µs | **0.43 µs** |
| `log.info("…", **kwargs)` | 10.48 µs | **1.78 µs** |
| structlog, `ReturnLogger` | 2.59 µs | 2.59 µs |
| stdlib, `NullHandler` | 2.96 µs | 2.91 µs |
| stdlib, `StreamHandler` to a discarding stream | 3.98 µs | 3.92 µs |
| stdlib, `QueueHandler` | 5.21 µs | 5.15 µs |

The before column is the answer to "was the fast tier fast?", and it was not:
2.55 µs is structlog's number, and the *default* redaction disposition made it
3.5× worse than structlog, because `SiteRegistry.fingerprint` was calling a
pure-Python SipHash-2-4 on the request path while an identical one sat compiled
in `flight.c`. That is what justified the emitter.

The kwargs tier's 1.78 µs includes ~0.17 µs it does not have to spend: it
compares this call's inferred field types against the interned site's before
reusing that site's spec blob, because `intern_template` keys on template text
and the text does not pin the types. Without the comparison, a template first
seen with an int and later called with a string would pack the string against an
int declaration and lose it. `_logsite.specs_for` carries the reasoning.

**2. The cost of a disabled `DEBUG(...)` call.** The load-bearing one, because
failure-triggered logging assumes verbose instrumentation is affordable:

| | cost |
| --- | --- |
| `STEP(name, n)`, below `capture_level` | 0.07 µs |
| `if STEP: STEP(name, n)` | 0.04 µs |
| `log.debug("…", **kwargs)`, disabled | 0.11 µs |
| stdlib `logger.debug(…)`, disabled | 0.07 µs |
| stdlib `logger.isEnabledFor(DEBUG)` guard | 0.04 µs |
| structlog `.debug()`, filtered out | 0.19 µs |
| **`STEP(name, n)` buffered for promotion** | **2.99 µs** |

So the assumption holds for a *disabled* site — 70 ns, indistinguishable from
stdlib's, and `if SITE:` stays an escape hatch rather than becoming the
documented idiom. It does **not** hold for a buffered one. `LoggingConfig` ships
`capture_level=DEBUG` with `level=INFO`, so the shipped default *buffers* every
DEBUG record at 3.0 µs each, on a request whose Python path is 2.2 µs. Ten DEBUG
statements in a handler cost 29 µs per request, every one of them discarded on a
healthy request.

That is a real finding and it is deliberately left as one — see
[what is now the most expensive thing here](#what-is-now-the-most-expensive-thing-here).

**3. A LOG cell's ring publish against a COMPLETION cell's.** The plan expected
them to be near-identical and said a gap would mean the packing was wrong. It
was the packing:

| | before | after |
| --- | --- | --- |
| `LogCell.encode()` alone | 1165 ns | 1165 ns |
| `publish_log(pre-encoded)` | 45 ns | 45 ns |
| encode + publish | 1215 ns | 1215 ns |
| **`Recorder.log(...)`, pack + publish in C** | — | **130 ns** |
| completion (`begin`/`route`/`finish`) | 205 ns | 205 ns |

The publish was always near-identical — 45 ns against a completion's 205 ns,
which does more. The packing above it cost 26× the publish. It now costs less
than a completion cell does, which is where the plan expected it to land.

**4. Projector drain with log cells mixed in.** Per cell, one `poll()` over a
pre-filled ring:

| logs per request | ns/cell | cells/s |
| --- | --- | --- |
| 0 | 4637 | 0.22 M |
| 1 | 4862 | 0.21 M |
| 10 | 4158 | 0.24 M |
| 100 | 4438 | 0.23 M |

The mix does not matter — a log cell costs the projector what a completion cell
costs it — which answers the question as asked. It also exposes a different one:
**~4.4 µs per cell is the projector's absolute throughput, about 230,000
cells/s, on a thread that shares the GIL with the loop.** At 30,000 rps with one
record per request that is roughly a quarter of a core taken from the request
path. This is now the largest cost in the subsystem, and it is not on the
request path — which is the trade the design intended — but it is a ceiling, and
it is written down here so the next person meets it as a number rather than as a
mystery.

**5. Request latency with logging on and off.** In-process ablation over the
whole Python request path, logging-off base = 2.16 µs:

| arm | before | after |
| --- | --- | --- |
| logging on, handler silent | +0.76 µs | +0.76 µs |
| logging on, 1 INFO record | +3.60 µs | **+1.33 µs** |
| logging on, 5 INFO records | +14.00 µs | **+3.16 µs** |
| logging on, 3 canonical fields | +6.26 µs | **+4.12 µs** |
| logging on, 5 buffered DEBUG | +13.43 µs | +14.29 µs |

**The socket arm did not resolve, and should not be quoted.** `--suite e2e`
boots wreath's own server, drives it with the built-in development load
generator, and interleaves logging-off / logging-on / logging-off. Within a
single run the A/A control is tight (~3%) and the delta looks resolved. Across
runs it is not: three runs gave

| run | build | off | on | delta |
| --- | --- | --- | --- | --- |
| A | Python emitter | 35,286 rps | 27,028 rps | −23% |
| B | native emitter | 38,195 rps | 31,983 rps | −16% |
| C | native emitter | 40,563 rps | 29,481 rps | −27% |

B and C are the *same build*, and they disagree by more than B disagrees with A.
The generator shares a process with the server, so both arms compete with the
client for the same cores and the same GIL, and the between-run spread swamps
what is being measured. The retained JSON keeps the rows; the honest reading is
that this configuration cannot resolve the question, and the in-process ablation
above — A/A floor 0.0–0.4%, reproduced across three runs — is what does. An
independent generator on a separate machine would settle it; that is the
"publishable results" rule in AGENTS.md, and it has not been done here.

What the socket runs do show consistently is p99 roughly doubling with logging
on (6.8 → 14.8 ms in run C, 9.9 → 17.7 ms in run B). That is measurement 4
appearing end to end: the projector and writer threads have cells to decode,
assemble and render, and they contend for the GIL with the loop. The request
path is no longer where logging costs.

**6. `MemoryBudget.logging`'s per-entry constants.** Measured against the tables
they describe, at capacity 4096:

| component | constant was | measured | constant now |
| --- | --- | --- | --- |
| interned site | 512 B | 426 B | 448 B |
| limiter slot | 96 B | 153 B | 160 B |
| queued record | 256 B | 406 B | 416 B |
| buffered record | 256 B | 335 B | 352 B |

Three of the four were low, which is the dangerous direction for a budget. The
docstring's claim that the estimate "becomes exact when the emitter moves to C"
was wrong and has been removed: these describe Python *tables*, and the emitter
was never the tables.

## The native emitter

`Recorder.log(site_id, severity, request_id, flags, dropped_siblings, specs,
values, k0, k1)` packs one record straight into a 64-byte cell and publishes it.
`METH_FASTCALL`, so the request path does not build an argument tuple; the
return value carries the publish result in bit 0 and the type-mismatch count
above it, so the call allocates nothing.

Three things stage 1 built for this made the swap mechanical rather than a
redesign, exactly as it claimed: a dense `site_id`, argument types declared at
the call site, and a level check that precedes marshalling. The declared types
reach C already flattened into a **spec blob** — one byte per field,
`(type << 4) | disposition`, computed once at registration — so packing branches
on a small integer instead of on `isinstance` and a disposition comparison.

The fingerprint key travels in the call rather than being read off the worker.
The pure packer hashes with the site registry's key, and a fingerprint that
differed between the two halves of one process would break correlation within a
single recording with nothing raising to say so.

**The pure path is the twin, not a fallback** (ADR 0005).
`tests/test_logging_native_parity.py` drives both over a corpus weighted towards
where two independent implementations drift — a bool that is also an int, an int
one past the wire slot, a string exactly on the clip boundary, a multi-byte
character straddling it, lone surrogates, bytes that are not valid UTF-8, more
arguments than a cell holds, fewer arguments than the site declares — and
requires the 64 bytes *and* the mismatch count to be identical.

Writing that corpus found three defects in the pure packer, all the same shape:
a value the wire cannot carry reached `struct.pack` or `str.encode` and **raised
out of `pack_value`**, into whatever made the log call. `pack_value`'s own
docstring says that must never happen. An int wider than int64, a float too wide
to narrow, and a lone surrogate are each a counted mismatch now.

The pure path still runs, by design, in three cases: no recorder (a test
capture, `testing_runtime`, a plain callable sink), a buffered record, and a
record made off the loop.

## Off-loop emission

The ring has exactly one writer: `ring_publish` reads the head, copies a cell,
and stores the head back with no interlock, because by construction one thread
does it. A `wreath.jobs` worker or a thread-pool task calling a log site is a
second writer, and two interleaved do not lose a record — they overwrite one and
advance the head anyway, corrupting every cell after it. That was reachable
before this change; it is not now.

An off-loop record is packed on the pure path, staged in
`_logscratch.OffLoopStage` (bounded, locked, drop-and-count, oldest kept), and
published by the loop on its next tick — `Server._drain_off_loop_logs`, a
`call_later` chain on the writer's own interval, plus one final drain during
shutdown before the runtime is swapped out. Records arrive one interval late
carrying `LOG_FLAG_OFF_LOOP`, so a reader can tell a late record from a
reordered one. Overflow is `LossReason.LOG_OFF_LOOP`, readable through
`logging.off_loop_counts()`.

Deliberately not per-thread staging buffers: those would order records by
whichever thread flushed first, which is a permanent tax on every record to
serve the rare one.

`LogRuntime.bind_writer()` is what opens the path, and the server calls it on
the loop during startup. Until it is called there is no thread check at all — a
sink that is not a ring has no single-writer rule to keep, and every test
capture and plain-callable sink is in that state.

## What is now the most expensive thing here

**A buffered record costs 3.0 µs, and the shipped default buffers.** The
decomposition, measured directly:

| | cost |
| --- | --- |
| `LogArg.integer(7)` — a frozen slots dataclass, 4 fields | 559 ns |
| `LogArg.text("validate")` | 553 ns |
| `pack_value` ×2 (two arguments) | 2324 ns |
| `LogCell(...)` construction | 700 ns |
| `LogCell.encode()` | 1225 ns |
| `dataclasses.replace(cell, …)` — every promoted record | 1561 ns |

It is not the packing logic. It is **object construction**:
`@dataclass(frozen=True, slots=True)` generates an `__init__` that calls
`object.__setattr__` once per field, and that is 550–700 ns for these shapes.
The same cost is the likely majority of measurement 4's 4.4 µs per projector
cell, since `CompletionCell`, `CorrelationCell` and `LogCell` all decode into
frozen slots dataclasses.

This is left as a diagnosis rather than a change, because it is not logging's
decision alone to make: every cell type in `_flight_schema` has the same shape,
`frozen` is a documented property of those value objects, and the projector is
the larger beneficiary. The next person should decide it deliberately, with
these numbers in hand, rather than inherit it. Reproduce with
`uv run python -m benchmarks.bench_logging --suite disabled publish drain`.

## Not done, and why

**Crash forensics (stage 7).** The ring is anonymous memory, so a segfault takes
the last records with it. The framing is ready for a file-backed ring — every
cell is versioned and every decode validates lengths against the buffer — so
adding it is not a format break.

**`wreath.audit`.** Keeps its own `logging.getLogger` path. "Never blocks the
request path" and "never loses a record" are incompatible promises; audit needs
the second and must not inherit the first.

**A second machine.** Every number above is one machine, one governor setting.
The rules in `src/wreath/_devtools/measure.py` bound the *noise*, not the
generality; nothing here should be quoted as a portable figure until it has been
reproduced somewhere else.
