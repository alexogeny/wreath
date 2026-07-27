# Native GIL avoidance and parallelism investigation plan

**Status:** companion investigation plan; no concurrency architecture is approved.

**Date:** 2026-07-18

Related:

- `docs/plans/native-performance-drain-audit.md`
- `docs/plans/worker-tape-architecture-baseline.md`
- `docs/plans/future/16-native-event-loop.md`
- `src/wreath/_devtools/native_gil_lint.py`
- `src/wreath/_devtools/measure.py`

## Review verdict

The safety model, process-worker recommendation, and measurement discipline in this plan are legitimate. The candidate list is not a list of confirmed defects: native compression does not exist yet, general PostgreSQL/JSON/template work would require major two-phase redesigns, and a native worker pool is premature. The only current pure kernels worth initial measurement are large WebSocket masking and HPACK Huffman decode, and neither is presumed to win.

A crucial distinction is that releasing the GIL on the event-loop thread does **not** let another coroutine on that same event loop run. It can permit Python on other OS threads to progress, but same-loop responsiveness requires returning to the loop, chunking work, or explicitly offloading it. Any fairness experiment must therefore distinguish same-loop delay from cross-thread GIL contention.

## Goal

Determine where Wreath can avoid GIL serialization or reduce its effects without slowing the small-request hot path, violating Python object lifetime rules, weakening ASGI semantics, or introducing hidden global executors. Treat normal CPython, free-threaded CPython, and the optional JIT as separately measured modes.

The immediate observation is simple: the reviewed native sources contain no `Py_BEGIN_ALLOW_THREADS` regions. That is not automatically a defect. Most request-path C code calls the Python C API frequently, and releasing/reacquiring the GIL for small work would add overhead. The useful question is narrower: **which bounded bulk kernels can operate entirely on owned native memory long enough for GIL release or parallel execution to pay?**

## Available strategies

### 1. Release the GIL around a pure C kernel

This is the lowest-complexity option when one call already contains substantial CPU work.

Required shape:

1. While holding the GIL, validate arguments, acquire strong owner references, export immutable buffers, check sizes/overflow, and allocate native/output storage.
2. Enter `Py_BEGIN_ALLOW_THREADS` only around code that makes no Python C API calls, does not dereference mutable Python object internals, does not raise Python exceptions, and does not invoke callbacks that may enter Python.
3. Return a native status/error record from the kernel.
4. Reacquire the GIL, release exports, construct Python objects, translate errors, and commit state.

Plausible candidates:

- large WebSocket mask/unmask XOR loops in `ws.c`;
- native compression/deflate work, using the measured threshold already contemplated by the compression plan;
- large HPACK Huffman decode if input, decode tree, and output storage are fully native and immutable;
- bulk byte scanning/copying in protocol or multipart code when no Python object is touched;
- selected PostgreSQL binary decode kernels that can first produce a native tape rather than Python values.

Poor candidates:

- routing and policy predicates, because calls are short;
- middleware coordination and handler activation, because they immediately call Python;
- JSON encoding of arbitrary Python objects, template rendering, ORM identity-map work, and validation over Python containers, because traversal repeatedly touches Python objects;
- nonblocking socket operations owned by asyncio, because they do not perform long blocking waits in these functions.

Use a payload threshold. The threshold must come from crossover measurements, not a guessed constant. Measure 1 KiB through at least 16 MiB and require repeated separation above the A/A floor.

### 2. Split object traversal from a native compute tape

Some operations cannot release the GIL today because Python traversal and byte computation are interleaved. They may be converted into two phases:

- **compile/materialize under the GIL:** walk Python values once and build a bounded native instruction/value tape with strong references or copied scalar bytes;
- **execute without the GIL:** run parsing, escaping, hashing, compression, encoding, or comparison only over native tape/buffers;
- **commit under the GIL:** construct result objects or apply changes.

Potential applications:

- template rendering after startup compilation, if dynamic values can be flattened without calling `__str__`, `__html__`, mappings, or descriptors in the no-GIL phase;
- JSON decode into a bounded native token/tape, followed by Python object construction;
- PostgreSQL row parse into field spans/type instructions, followed by Python scalar/model construction;
- batched authorization masks or routing candidates represented as primitive arrays.

This strategy can double peak memory or add a complete pass. It is justified only when the current GIL-held phase demonstrably harms p99 fairness or free-threaded throughput. Never retain borrowed references in a tape crossing the released region.

### 3. Use a bounded native worker pool for coarse CPU jobs

A native pool can execute pure C jobs concurrently while the event-loop thread remains available. Completion must be delivered back to the owning loop with a thread-safe scheduling mechanism, and Python objects must be created or mutated only after the worker result returns to a Python-attached thread.

Use only for coarse, explicitly asynchronous jobs such as:

- multi-megabyte compression;
- expensive password/KDF or signature verification implemented by a library that releases/does not require Python;
- large schema/type-generation rendering if it ever becomes latency-sensitive;
- independent bulk decode/encode batches.

Do not use it for routing, header parsing, request construction, or small JSON. Queueing, synchronization, cache misses, and wakeups will dominate.

The pool must be application/server-owned, bounded by jobs and bytes, and have explicit overload behavior. It must not be a hidden process-global singleton. Cancellation is cooperative: cancellation can stop unsubmitted work and suppress delivery, but cannot pretend a running native kernel stopped unless that kernel supports interruption. Shutdown must join workers without holding the GIL in a way that deadlocks callbacks.

### 4. Use multiple worker processes

Process workers remain the most reliable way to bypass the GIL for arbitrary Python handlers. A Wreath server can supervise N workers and distribute accepted connections with `SO_REUSEPORT`, inherited listeners, or a parent acceptor.

Advantages:

- arbitrary user Python runs in parallel;
- crash and memory isolation;
- no requirement that every extension be free-thread safe.

Costs:

- duplicated application/cache memory;
- startup and graceful-drain coordination;
- database pool multiplication;
- connection ownership and load imbalance;
- state must be external or explicitly replicated.

This should be the recommended production answer for CPU-bound Python handlers until free-threaded mode is proven. It is server architecture, not a C micro-optimization, and should align with the future event-loop/supervision plans.

### 5. Support free-threaded CPython as a separate mode

Python 3.14 free-threaded builds can allow Python handlers to run concurrently, but removing the global GIL does not make current extension state automatically safe. Before that audit even matters, an extension that has not declared no-GIL support may cause CPython to enable the GIL when imported. Wreath's current single-phase extension initializers do not declare no-GIL support, and declaring it before the audit would be unsafe.

Run `uv run wreath-native-gil-status` on a free-threaded build before every benchmark. Its `--check` mode fails unless the process observably starts with the GIL disabled and keeps it disabled across the native-package import. Passing is only a transition check, not proof of data-race safety.

Wreath must then audit:

- module-static mutable caches and type/configuration globals;
- lazy initialization;
- reference ownership and borrowed references across concurrent calls;
- router/registry publication and mutation;
- connection/protocol objects that assume one event-loop thread;
- freelists, counters, and error state;
- third-party nghttp2/nghttp3/ngtcp2/OpenSSL/zlib thread-safety and object ownership.

Prefer immutable startup compilation followed by atomic publication, per-application/per-interpreter ownership, and connection confinement to one loop thread. Add locks only around shared mutation; coarse locks around every native call would recreate the GIL with more overhead.

Benchmark free-threaded mode independently with one and many threads. Report single-thread regression as well as scaling, p95/p99, CPU, and memory. Do not claim benefit merely because two threads execute.

### 6. Consider subinterpreters only after extension isolation

Per-interpreter execution can isolate Python heaps and may provide parallelism depending on build/runtime configuration, but the current extensions use process-global module/type/configuration state in several places. Some module definitions use `m_size = -1`, which is a warning sign for subinterpreter isolation.

Before considering request or worker placement in subinterpreters:

- move mutable module globals into module state;
- use multi-phase initialization where appropriate;
- declare and test interpreter/GIL support accurately;
- prohibit Python objects from crossing interpreter boundaries;
- define serialization/ownership for application configuration and results;
- test repeated interpreter create/destroy under ASan/UBSan and leak checks.

This is a long-term architecture option, not a near-term hot-path fix.

### 7. Keep blocking work out of the event-loop thread

GIL avoidance is irrelevant if the event-loop thread blocks in filesystem, DNS, entropy, certificate, or subprocess work. The native GIL lint already flags likely blocking native I/O while holding the GIL (`NG002`). Continue using nonblocking sockets and explicit async ownership. For genuinely blocking operations:

- release the GIL around a safe blocking C call; or
- route the operation through an explicit bounded executor/service.

Do not offload merely to hide a synchronous design. Account for queue wait, cancellation, byte bounds, and shutdown.

## Safety rules

The existing GIL lint encodes minimum invariants:

- `NG001`: no Python C API while the GIL is released;
- `NG002`: no genuinely blocking native I/O while holding it;
- `NG003`: no borrowed Python reference crossing a released region—take a strong reference first;
- `NG004`: balance every `PyGILState_Ensure`/`PyGILState_Release` path;
- `NG005`: native thread callbacks acquire the GIL before Python API use.

Additional rules for any implementation:

- Keep every exported buffer owner alive until the GIL is reacquired and the kernel is finished.
- Never resize a Python bytes/bytearray/list backing store while a worker or library retains a pointer.
- Do not call allocator APIs that require an attached Python thread from a released region; use clearly permitted native allocation or preallocate.
- Store native error codes/messages during the released phase; set Python exceptions only after reacquisition.
- Protect mutable native instance state against concurrent/reentrant calls with an explicit state machine.
- Avoid waiting for a worker while holding the GIL if worker completion needs the GIL.
- Make finalization robust when the loop, interpreter, or application closes before a job completes.

## Measurement program

### Responsiveness and contention harnesses

Use two distinct experiments; do not interpret one as the other.

**Same event-loop responsiveness:** run one bulk operation alongside a 1 ms heartbeat, minimal requests, and cancellation/disconnect work on the same loop. This prices how long the native call prevents the loop from regaining control. Merely releasing the GIL inside that synchronous call cannot improve this result; chunking or worker offload can.

**Cross-thread GIL contention:** run the bulk native operation in one Python thread while an independent Python thread increments a calibrated counter or serves work on its own loop. This determines whether a released kernel lets other interpreter threads progress. Pinning/affinity and the observer's own overhead must be recorded.

For both, record operation throughput, heartbeat or observer progress, request p50/p95/p99 where applicable, CPU, context switches, and peak RSS. Compare current implementation, candidate release/offload implementation, and no-op/loop-floor controls.

### Crossover sweep

For each candidate kernel, sweep input sizes and concurrency. Include at least:

- 1, 4, 16, 64, and 256 KiB;
- 1, 4, and 16 MiB;
- compressible/incompressible, valid/invalid, aligned/unaligned, and cancellation cases as relevant.

Choose the release/offload threshold where repeated total-system results separate, not where a standalone kernel first looks faster.

### Parallel-mode matrix

Measure separately:

1. standard CPython, one event-loop thread;
2. standard CPython, multiple process workers;
3. free-threaded CPython, one thread;
4. free-threaded CPython, multiple threads;
5. optional JIT on/off where available.

Use equivalent workloads and record build/runtime metadata. A result in one mode must not be generalized to another.

## Ordered next-agent evidence pathways

1. **Free-threaded import status** — run `wreath-native-gil-status --json` and `--check` on a free-threaded CPython 3.14 build. If import enables the GIL, inventory every module initializer and process-global `PyObject *` before considering module no-GIL declarations.
2. **WebSocket mask/unmask crossover** — benchmark 1 KiB through 16 MiB in two-thread contention and same-loop responsiveness harnesses. A release experiment must allocate/retain output safely and detach only around `xor_mask`; reject it if ordinary frame throughput regresses or cross-thread progress does not materially improve.
3. **HPACK Huffman crossover** — extend `benchmarks/bench_hpack_decode.py` with encoded-size sweeps and a second-thread observer. Allocate the native output while attached, detach only for the transition-table loop, and perform `PyBytes` construction/freeing after reattachment. Respect header limits and reject below-noise results.
4. **Native compression, if implemented later** — this is the strongest conventional future candidate, but there is no current native compressor to change. Establish a threshold with compressible and incompressible inputs before adding release regions.
5. **PostgreSQL isolated kernels** — start with large hexadecimal `bytea`, not general row hydration. General batch decode touches Python per field and requires a separately approved native-tape design.
6. **JSON/template tapes** — proceed only after an ablation shows a large GIL-held phase and includes the extra-pass/peak-memory cost. These are architecture projects, not release-macro edits.
7. **Bounded native CPU service** — consider only if at least two coarse kernels prove queueable work; otherwise use explicit existing executors or process workers.
8. **Subinterpreters** — defer until module state and extension initialization are interpreter-isolated.

## Rejected shortcuts

- Releasing/reacquiring the GIL per header, route segment, row field, or ASGI chunk.
- Calling Python callbacks from a released region.
- Passing borrowed list/dict/tuple items to a worker.
- A hidden global thread pool.
- Unbounded executor queues.
- Treating a native event loop as a way to make Python handlers GIL-free.
- Assuming free-threaded CPython makes connection objects safe to call from multiple threads.
- Using thread count alone as evidence of parallel speedup.

## Evidence recorded during implementation review

### WebSocket masking: release experiment rejected

`benchmarks/bench_native_gil.py` now measures uncontended latency separately from a CPU-bound Python observer thread. A controlled A/B/A experiment used 1 MiB and 16 MiB payloads with 5 warmups and 15 trials on CPython 3.14.6/Linux x86-64.

The two no-release A runs were stable at 1 MiB: uncontended medians were 27.2 and 27.4 microseconds, contended worker medians were 48.2 and 48.3 microseconds, and the observer made no progress during the call. Releasing at 1 MiB let the observer run hundreds of thousands of iterations, but the worker commonly waited for the CPU-bound observer's GIL scheduling quantum when reattaching: the 1 MiB median became about 5.12 milliseconds. Uncontended latency remained neutral at about 25.6 microseconds. The 16 MiB release trials similarly moved from roughly 1–2 milliseconds without release to roughly 6–8 milliseconds under contention while enabling observer progress.

That is a real concurrency/latency trade-off, not an assumed regression. Wreath prioritizes predictable request latency, and the release does not help the same event loop, so the C experiment was reverted. Keep the benchmark. Revisit only for an explicit multi-loop/thread deployment mode with a documented latency policy or for worker-offloaded bulk operations where reattachment is not on the request-critical thread.

### HPACK Huffman decode: too short to detach

The existing whole-protocol benchmark was rerun with 3 warmups and 9 retained trials. Median cases ranged from about 2.2 microseconds for common headers to 31.3 microseconds for the maximum 16 KiB ASCII case, including frame dispatch and stream creation. A GIL release inside only the Huffman loop has no credible budget here, and header limits bound the work. No C change is justified.

### Remaining pathways

- Native compression remains future work because no native compressor exists.
- PostgreSQL should start with an isolated large `bytea` experiment; general decode/hydration is Python-object-heavy.
- Same-loop stalls require chunking or worker offload, not `Py_BEGIN_ALLOW_THREADS` alone.
- Free-threaded support still begins with `wreath-native-gil-status --check`, followed by module-global/thread-safety audit before any no-GIL declaration.
- A native worker service remains deferred until at least two coarse kernels demonstrate queueable work.

## Completion criteria

The strategy is ready for further implementation when at least one remaining candidate has:

1. a reproducible fairness or throughput deficit above the A/A floor;
2. a Python-object-free kernel boundary with documented ownership;
3. a measured release/offload threshold;
4. red tests for reentrancy, cancellation, finalization, errors, and buffer lifetime;
5. a verification matrix for standard, free-threaded, and optional-JIT modes;
6. an explicit decision on application/server ownership and overload behavior.

Until then, multiple process workers are the safe general workaround for CPU-bound Python handlers, and selective GIL release around large pure C kernels is the preferred native optimization path.
