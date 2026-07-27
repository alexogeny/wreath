# Worker/tape architecture decision review and baseline

**Status:** architecture review and proposed evidence baseline; no implementation approved.

**Date:** 2026-07-18

Related:

- `docs/plans/native-gil-strategy.md`
- `docs/plans/background-tasking.md`
- `docs/plans/future/03-supervised-services.md`
- `docs/plans/future/16-native-event-loop.md`
- `docs/plans/browser-security-cache-native-compression.md`
- `src/wreath/_devtools/measure.py`

## Executive decision

Wreath should **not build a native worker pool yet**. The current codebase has the lifecycle and pressure-control conventions needed to host one, but no measured kernel presently clears the admission gate:

- WebSocket masking benefits another Python thread when the GIL is released, but the measured request-thread reattachment latency is unacceptable and the same event loop does not progress.
- HPACK's bounded maximum case is about 31 microseconds end to end, too small to offload.
- The proposed direct-zlib extension was already rejected by its retention gate; production compression intentionally uses CPython's maintained `zlib` module.
- General PostgreSQL hydration, JSON encoding, and template execution remain interleaved with Python objects and are not worker-safe kernels without a separately justified tape conversion.

The sensible baseline is therefore a **three-gate program**:

1. prove same-loop blocking with the existing implementation;
2. prove that a bounded, application-owned Python executor improves total-system tails enough to justify lifecycle complexity;
3. build a native worker/tape backend only if the Python executor's scheduling, Python-frame, or memory cost is itself measured and material.

This avoids creating a second scheduler before Wreath has work worth scheduling.

## Current codebase constraints

### Application lifecycle is the portable ownership seam

`Wreath._lifespan()` already starts application-owned databases and HTTP clients, rolls them back in reverse order after partial startup failure, and closes them during lifespan shutdown. This works behind Wreath's server and conforming third-party ASGI servers. A worker facility intended for framework operations must preserve that portability.

The current lifecycle is explicit but specialized: databases and HTTP clients have dedicated collections, while arbitrary startup/shutdown handlers are unstructured callbacks. The future supervised-services plan correctly rejects untracked long-lived tasks and assigns readiness, failure, cancellation, and diagnostics to application-owned services.

**Consequence:** a production worker facility belongs to the application lifecycle, not a process-global singleton and not only to `wreath.server`. The server may supply an optimized wakeup/completion adapter later, but it must not be the sole owner.

### Server shutdown already provides the right outer ordering

`Server.close()` stops accepting connections, asks protocols to stop accepting requests, drains active responses until `shutdown_timeout`, closes remaining transports, then runs lifespan shutdown. That is the correct outer sequence for application workers: stop new request submissions first, drain request-owned work, then close the application facility.

Third-party servers control their own drain ordering, so the worker must still reject submissions as soon as lifespan shutdown begins and tolerate request cancellation racing shutdown.

### Existing background tasks are not the worker facility

`BackgroundTask` is response-bound work. Synchronous callbacks use `asyncio.to_thread`, remain part of the ASGI invocation, and are awaited after response emission. The documentation explicitly says that request injection, retries, priorities, result APIs, and application-owned workers are a different subsystem.

The default asyncio executor is useful as a comparator, but it is not Wreath-owned, has no Wreath byte budget, and does not expose its queue/readiness state through application lifecycle diagnostics.

### Existing pressure conventions are strong

Wreath already prefers explicit count and byte bounds:

- request bodies default to 16 MiB;
- multipart has per-part, aggregate-memory, and field-count limits;
- HTTP clients bound connections, keepalive entries, waiters, headers, and response bytes;
- PostgreSQL pools bound size and acquisition queues;
- native protocol plans use high/low-water backpressure and bounded retained bytes.

A worker facility must follow the same pattern. A job-count limit alone is insufficient because four 16 MiB jobs and four 1 KiB jobs have radically different pressure.

### Compression is not a blank-slate native candidate

Current compression delegates to CPython's maintained native `zlib` module. The planned direct-zlib extension was implemented experimentally and rejected by a measured retention gate. A future worker study should ask whether **offloading** large stdlib compression improves event-loop tails, not reopen the rejected “rewrite zlib glue in Wreath C” question.

## Decision baseline

### 1. Ownership

**Baseline decision: application-owned, one facility per `Wreath` application per process, created only when explicitly enabled.**

Rationale:

- preserves operation behind any conforming ASGI server;
- matches database and HTTP-client ownership;
- gives startup failure and shutdown a clear owner;
- avoids a hidden process-global executor;
- permits different applications in one process to have different limits;
- leaves room for a Wreath-server completion adapter without coupling the framework to its server.

Not selected:

- **server-owned:** breaks portability and makes the same application behave differently on Uvicorn or another ASGI server;
- **module-global:** complicates interpreter finalization, testing, fork behavior, multiple applications, and future subinterpreter work;
- **request-owned:** repeats thread creation and cannot enforce aggregate pressure.

The application should expose configuration, health, and counters, but worker internals should not become request state or a top-level global API.

### 2. Initial capacity and bounds

**Baseline decision: disabled by default. For an evidence prototype, use deterministic conservative limits rather than CPU-count autotuning.**

Proposed prototype envelope:

| Limit | Baseline |
| --- | ---: |
| Workers | 2 |
| Running plus queued jobs | 4 |
| Maximum input per job | 16 MiB |
| Maximum aggregate charged input | 64 MiB |
| Maximum result per job | 32 MiB |
| Maximum aggregate completed-but-undelivered result bytes | 64 MiB |
| Admission wait | none by default |
| Shutdown drain deadline | application-configured, default 10 s to align with current server/database conventions |

These are experiment defaults, not promised public defaults. Two workers are enough to expose concurrency and lifecycle defects without turning a benchmark into CPU-count scaling work. The 16 MiB input aligns with the current request-body default. Separate input and completed-result budgets prevent a slow event loop from accumulating worker output.

Every admitted job is charged for owned input immediately. It remains charged while running, even if its awaiter is cancelled. Result bytes are charged until delivered or discarded. Metadata has a fixed per-job accounting charge so empty/tiny jobs cannot bypass pressure through object count.

### 3. Overload behavior

**Baseline decision: immediate explicit rejection; never silently execute synchronously on the event-loop thread.**

A synchronous fallback would recreate the exact tail-latency failure that motivated offload and make overload behavior non-deterministic.

The low-level facility should report a distinct saturation outcome. Kernel adapters then choose semantics:

- optional response compression may send identity when HTTP negotiation permits it;
- an explicit “compress this payload” API raises a stable capacity exception;
- internal best-effort telemetry may drop with a counter;
- required validation/verification must reject rather than skip.

Waiting for admission may be offered only as an explicit mode with its own bounded waiter count and deadline. It is not the default.

### 4. Cancellation

**Baseline decision: queued jobs are removable; running jobs are abandoned, not force-killed.**

- If cancellation wins before dequeue, remove the job, release all charges, and resolve no completion.
- Once native execution starts, cancellation marks the delivery abandoned. The kernel runs to a safe boundary, its result/error is discarded on the owning loop, and all byte charges remain until cleanup completes.
- Cancellation never claims that native work stopped when it did not.
- A worker never decrefs Python owners or completes an asyncio future directly.
- Completion and cancellation race through one generation/state transition so delivery happens at most once.

Kernels admitted to the facility must have a bounded worst-case runtime or an explicit cooperative cancellation flag checked at documented intervals. A library call that may block forever is not admissible.

### 5. Shutdown

**Baseline decision: reject, drain, join; native threads may not outlive application lifespan or interpreter finalization.**

Proposed order:

1. mark the facility stopping and reject new submissions;
2. cancel/remove queued jobs;
3. mark running jobs abandoned unless their request still belongs to the server's active drain window;
4. wait up to the configured deadline for running kernels;
5. join every worker;
6. discard undelivered results on the event-loop thread;
7. publish stopped state and counters.

C threads cannot be safely killed. Therefore a shutdown deadline cannot mean “detach the threads and continue interpreter teardown.” If the deadline expires, shutdown fails loudly and the enclosing process supervisor may terminate the process. This is another reason to admit only bounded kernels.

Startup must be atomic: if any worker or wakeup primitive fails, stop/join those already created before lifespan startup reports failure.

The facility should eventually use the supervised-services lifecycle rather than hiding inside user shutdown handlers. Until that foundation exists, a prototype should have a dedicated internal lifespan slot ordered before user startup callbacks and after user shutdown callbacks have stopped producing work, but before HTTP clients/databases are closed only if its kernels can depend on them. The first kernel should not have such dependencies.

### 6. First proof kernel

**Baseline decision: use existing stdlib one-shot gzip compression as an offload proof, not a new native compressor and not yet a native tape.**

Why:

- input and output are immutable bytes;
- work is coarse and size-dependent;
- errors are simple;
- no application callbacks are involved;
- middleware can fall back to identity under saturation;
- the current direct implementation and retained compression benchmarks already exist;
- it tests same-loop responsiveness, the reason a worker is considered.

The proof should first use a dedicated, bounded Python `ThreadPoolExecutor` or equivalent application-owned executor around the existing `gzip_compress`. Compare it against direct stdlib compression and process offload. Only if the bounded thread version wins total-system tails should a native worker be considered.

Do not use HPACK as the proof: measured work is too short. Do not use WebSocket framing first: protocol ordering, read pausing, frame ownership, and close semantics obscure the worker question. Do not use ORM hydration first: Python object construction and identity maps obscure the kernel boundary.

### 7. Minimum operation size

**Baseline decision: no fixed production threshold until a crossover study; initial sweep from 64 KiB through 16 MiB.**

Measure at least 64 KiB, 256 KiB, 1 MiB, 4 MiB, and 16 MiB for compressible and incompressible data at representative gzip levels. For every size compare:

- direct execution;
- bounded dedicated thread offload;
- process offload;
- loop-floor/no-op submission;
- saturation with all workers busy.

Record throughput, request p50/p95/p99, heartbeat max/p99, queue wait, execution time, completion-delivery delay, CPU, context switches, compressed size, errors, cancellations, and peak RSS. Establish A/A noise for each mode.

Admit offload only where repeated total-system results show a clear crossover. A provisional threshold must be rounded upward to a stable size boundary and remain configurable/internal until multiple workloads reproduce it.

### 8. Why existing executors or processes might be insufficient

**Baseline decision: they are not presumed insufficient; they are mandatory comparators.**

`asyncio.to_thread`/the loop executor can prove whether returning to the event loop matters, but it lacks application-owned queue bytes, lifecycle diagnostics, and deterministic shutdown policy. A semaphore acquired before submission can bound active/submitted work and should be tested before creating a native queue.

A dedicated `ThreadPoolExecutor` adds application ownership and shutdown but still runs Python entry/exit code and does not provide byte-aware admission by itself. A thin application-owned admission layer may be sufficient permanently.

Process workers support arbitrary CPU-bound Python and isolation, but large-byte IPC/serialization, duplicated memory, process lifecycle, and result transfer may dominate compression-like kernels. They remain the production baseline for arbitrary user Python.

A native worker is justified only if the dedicated bounded thread executor proves the workload and then native submission/completion removes a separately measured material cost. “C should be faster” is not approval evidence.

## Tape baseline, if a kernel later requires one

A tape is not a generic Python object graph and must not retain borrowed Python references. The baseline representation should contain:

- ABI/version and kernel opcode;
- one owned native allocation or bounded set of owned spans;
- offsets and lengths rather than pointers into resizable storage;
- primitive configuration values;
- cancellation generation/state;
- input and maximum-result charges;
- native error code plus bounded diagnostic bytes;
- no callable, exception, asyncio object, mapping, iterator, or model instance.

Preparation occurs on the owning event-loop thread. Execution reads immutable native storage. Completion returns a native result descriptor. Python result construction and exception translation happen on the owning event-loop thread.

For decode workloads, separate the phases explicitly:

1. worker validates/parses bytes into a bounded token/span tape;
2. event-loop thread materializes Python strings, numbers, containers, records, or models.

The second phase must be measured. A tape that merely moves half the work off-thread while doubling peak memory is not a win.

## Required state machine and observability

Minimum job states:

```text
CREATED -> QUEUED -> RUNNING -> COMPLETED -> DELIVERED
                    |          |            |
                    |          +-> DISCARDED
                    +-> ABANDONED -> DISCARDED
QUEUED -> CANCELLED
```

Every transition is single-owner or synchronized. Counters should include:

- submitted, rejected by count, rejected by bytes;
- queued, running, completed, delivered;
- cancelled before run, abandoned while running;
- input bytes charged, result bytes charged, high-water values;
- queue wait and execution histograms;
- completion-delivery delay;
- worker failures and shutdown deadline failures.

Diagnostics must be bounded. Flight Recorder integration may record lifecycle IDs and aggregate counters, but production hot paths should not allocate per-job tracing objects by default.

## Stop/go gates

### Gate 0 — no subsystem

Retain the current architecture if direct operations do not cause a measured same-loop tail problem. This is the current state.

### Gate 1 — bounded Python executor

Proceed only if direct large compression causes a repeatable responsiveness deficit and bounded thread offload improves it without unacceptable throughput, CPU, memory, or cancellation regressions.

### Gate 2 — native worker

Proceed only if Gate 1 wins and ablation shows Python executor submission/completion overhead is itself material. Write an ADR covering ownership, limits, overload, cancellation, shutdown, and first kernel before implementation.

### Gate 3 — tapes and additional kernels

Proceed one kernel at a time. PostgreSQL, JSON, templates, and WebSocket protocol work each require separate ownership/parity evidence and must not be bundled into generic tape machinery.

## Proposed ADR baseline

If Gate 2 is reached, the recommended ADR decision is:

> Wreath will use an explicitly enabled, application-owned, bounded native worker facility for coarse Python-independent kernels. It rejects saturation rather than falling back on the event-loop thread, removes queued cancellations, abandons but safely drains running cancellations, joins all workers during lifespan shutdown, and admits each kernel only above a measured crossover threshold. The first accepted kernel must have no callbacks or external-resource dependencies. Generic ASGI behavior and multi-process deployment remain supported.

Consequences:

- positive: portable ownership, predictable pressure, same-loop responsiveness, auditable no-Python worker boundary;
- negative: a second scheduler, memory accounting, shutdown complexity, cancellation that cannot stop every running library call, and additional tail modes;
- constraint: no hidden global pool, no unbounded queue, no synchronous overload fallback, and no worker interaction with Python objects.

## Immediate recommendation

Do not implement native workers or tapes now. Add the bounded-executor compression experiment to the existing compression benchmark family when this work is prioritized. If it fails Gate 1, publish the result and close the worker proposal until a different coarse kernel presents measured need.
