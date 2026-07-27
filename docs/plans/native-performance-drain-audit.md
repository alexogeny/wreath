# Native-path theoretical performance drain audit

**Status:** static audit and investigation plan; no optimization is approved by this document.

**Date:** 2026-07-18

Companion plan: `docs/plans/native-gil-strategy.md`.

## Purpose

Review Wreath's C extensions for plausible CPU, allocation, memory-retention, latency, and scheduler-fairness drains, then identify substantial Python work that remains on the Wreath native request path. Turn each credible observation into a reproducible investigation rather than treating source-level suspicion as a measured defect.

This report follows the repository rule: **measure the thing before building the fix**. A crossing count is structural evidence, not elapsed cost, and a plausible C micro-optimization is not a win until an end-to-end ablation clears a measured A/A noise floor.

## Scope and method

The audit covered the native module entry points and implementation files under:

- `src/wreath/_native/*.c`, including core routing, HTTP codecs, policy helpers, JSON, templates, validation, WebSocket framing, HTTP/1, HTTP/2, and HTTP/3;
- `src/wreath/_native/postgres/*.c`, including protocol ingestion, codecs, field tapes, hydration, records, and model storage;
- the Python native-path facades and request lifecycle in `src/wreath/app.py`, `request.py`, `response.py`, `binding.py`, `middleware/`, `http_client.py`, and `orm/`;
- the existing performance plans and the request-boundary/decomposition tooling.

The review looked specifically for front deletion, repeated compaction, non-geometric growth, incremental-parser rescans, per-item imports and named dispatch, repeated Python-object construction, avoidable Python/native transitions, retained buffers, missing backpressure, and long GIL-held loops. Focused reads were then used to reject or downgrade false positives.

Current structural checks:

- `uv run wreath-native-lint`: **0 findings across 65 files**.
- `uv run wreath-request-trace`: **131 calls into C and 137 Python frames total**; before route activation, **37 calls into C and 50 Python frames**.

The trace's pre-activation phase currently attributes 29 Python frames to middleware, 10 to auth, 6 to ingress, and 5 to routing. The most repeated Python frames are `Request.state` (7), `State.__setattr__` (6), and `Request.scope` (3). The most frequent C entries are generic operations rather than Wreath fused operations: `dict.get` (38), `list.append` (13), and `isinstance` (12).

## Executive assessment

The C code is substantially more deliberate than a naive accelerator: geometric growth is common, indexed queues have replaced front deletion, incremental HTTP/1 scanning has cursors, fixed strings are often cached, and PostgreSQL tapes compact in batches. The native lint being clean is meaningful. It does **not**, however, prove that the current architecture is at its optimum.

The highest-value unresolved questions are architectural rather than isolated instructions:

1. **The native server still enters a large Python application dispatcher before handler activation.** Routing helpers and policy primitives are native, but lifecycle orchestration, request wrapper access, state writes, middleware sequencing, auth coordination, response coercion, and exception/finalizer unwinding remain Python.
2. **HTTP/3 response sends acknowledge the application immediately while retaining immutable body chunks until transport acknowledgement.** A producer can therefore outrun the network and grow retained memory without ASGI-level backpressure.
3. **HTTP/2 and HTTP/3 construct fresh ASGI message dictionaries and awaitable/future objects per body event.** This is required at a generic ASGI boundary, but it may be avoidable when the native server and Wreath application explicitly negotiate a private native context.
4. **Several C paths repeatedly dispatch Python methods by string.** Most are cold lifecycle operations, but future completion in HTTP/2/3 can occur per body event and deserves isolation.
5. **Large payload loops generally run while holding the GIL.** That may maximize single-request throughput but can damage event-loop tail latency or free-threaded scaling. It is a hypothesis, not a blanket instruction to release the GIL.

The recommended first project is not “translate all Python to C.” It is to price the pre-activation lifecycle by ablation, add HTTP/3 retained-byte/backpressure measurements, and only then choose the smallest native seam that removes proven cost.

## Findings after self-review

### P0 investigation: HTTP/3 application backpressure is absent

**Evidence.** In `src/wreath/_native/http3_asgi.c:204-236`, every non-empty `http.response.body` bytes object is appended to `s->resp_chunks`, the stream is resumed/flushed, and a resolved future is returned immediately. `src/wreath/_native/http3_asgi.c:287+` releases segments only after nghttp3 reports acknowledged application bytes.

This ownership is correct for retransmission safety: pointers handed to nghttp3 cannot refer to a reallocated buffer. The drain is that application progress is not coupled to queued/acknowledged bytes. A fast app streaming to a slow peer can retain its whole response, plus list/reference overhead, despite using small ASGI chunks.

**Potential impact.** Peak RSS, allocator pressure, cache churn, and unfairness across streams/connections. This is primarily a pressure and latency risk, not necessarily a throughput loss in an unconstrained loopback benchmark.

**Self-review.** The initial file header says the response is buffered and submitted once complete, but the implementation now submits headers/data-reader immediately. That comment is stale; complete-response buffering is **not** the current defect. Immutable per-chunk retention is necessary. The unresolved defect is missing high/low-water backpressure on retained, unacknowledged bytes.

**Investigation.** Extend `benchmarks/bench_native_pressure.py` with an `h3-blocked-send` case:

- exhaust or severely constrain QUIC/HTTP/3 flow control;
- stream 16, 64, and 256 MiB as 4 KiB chunks;
- record chunks accepted by the app, retained response bytes, list length, bytes acknowledged, peak RSS, and time to cancellation/disconnect;
- repeat with 16 KiB and 64 KiB chunks to separate byte pressure from object pressure;
- include a normal-speed control and at least five measured trials after warmup.

**Candidate resolution, only if confirmed.** Add per-stream queued-byte accounting and one send waiter. `h3_asgi_send` should return an unresolved future above a high-water mark and resolve it below a low-water mark as acknowledgement callbacks release segments. Cancellation, reset, connection close, and app failure must resolve or fail the waiter exactly once. Do not copy or coalesce storage still exposed to nghttp3. Test ASGI ordering, final EOF, reset, cancellation, and retransmission lifetime under sanitizers.

### P0 investigation: Python lifecycle before handler activation

**Evidence.** `src/wreath/app.py:443-681` coordinates global hooks, classification, authentication, authorization, miss/static handling, route middleware, binding/handler invocation, response coercion, exception handling, global finalizers, emission, and background work. `src/wreath/middleware/base.py:104-125` interprets the route middleware tape in Python. `src/wreath/request.py:129+` remains a Python wrapper even when backed by the native `WreathRequestContext` in `src/wreath/_native/server_request.c`.

The current trace reports 50 Python frames and 37 C calls before route activation. In particular, it repeatedly crosses for native helpers while keeping control flow in Python. The native server's stated shape—stay native through ingress, routing, authentication, and authorization—is therefore not yet achieved structurally.

**Potential impact.** Coroutine creation/resumption, frame setup, generic mapping/property access, temporary object allocation, and repeated boundary traffic on every realistic request.

**Self-review.** The trace does not price these frames. Existing decomposition work has already shown that removing a frame or crossing can sit below noise, and prior profiling misidentified CSRF glue as expensive. Translating the dispatcher wholesale would be high-risk and could make exception/finalizer semantics opaque for no measurable gain.

**Investigation.** Add ablations to the shared measurement harness rather than using `cProfile`:

1. direct handler activation with a prebuilt `Request` and response;
2. route classification only;
3. classification plus each of proxy, rate limit, CORS, CSRF, request ID, timing, authn, and authz;
4. middleware tape interpreter versus a generated Python route runner;
5. normal response versus exception, denial, miss, static, and ingress short-circuit;
6. native server and a third-party ASGI server separately.

For each shape, report A/A floor, all trials, median and tails, frame/crossing deltas, and allocation/peak-RSS observations. Use `wreath-tape-decomp` for global middleware and `wreath-decomp` for lifecycle stages. A native seam is justified only for a cumulative group that repeatedly clears noise.

**Candidate resolution, only if confirmed.** Compile an immutable per-route execution descriptor at startup and expose a private native-server/Wreath fast-path protocol. Keep generic ASGI unchanged. The descriptor may perform only proven native stages, then create/activate Python at the handler boundary. Python hooks, custom auth backends, custom exception handlers, and arbitrary middleware must cause an explicit fallback or native-to-Python activation—not semantic drift. Preserve global-finalizer unwinding for every early exit.

### P1 investigation: per-body ASGI object and awaitable construction

**Evidence.** HTTP/1, HTTP/2, and HTTP/3 build fresh dictionaries for `http.request` and `http.disconnect`. HTTP/2 uses `Py_BuildValue` and wraps ready values as awaitables; HTTP/3 uses `Py_BuildValue`, creates/resolves futures, and returns resolved futures from sends. This work scales with ASGI chunk count rather than just bytes.

**Potential impact.** Allocation/refcount overhead and event-loop scheduling overhead for highly fragmented request or response bodies. Small JSON requests are unlikely to expose it; streaming and proxy workloads may.

**Self-review.** These objects are part of the public ASGI contract and cannot be removed from the generic path. Reusing mutable dictionaries across awaits would be incorrect. Chunk coalescing can alter backpressure and delivery timing. This is therefore an optimization opportunity only behind a private negotiated Wreath path.

**Investigation.** Add a chunk-size sweep (1 byte through 64 KiB at fixed total bytes) for HTTP/1, HTTP/2, and HTTP/3. Record allocations, Python frames, total time, p95/p99 event-loop delay, and bytes retained. Compare:

- generic ASGI application;
- Wreath `Request.body()` consumption;
- streaming Wreath handler;
- a test-only native-context receive primitive that returns body/flags without a dict.

**Candidate resolution.** Extend `WreathRequestContext` with native receive/send operations consumed directly by `Request`/`Response` when available, while lazily materializing exact ASGI messages for arbitrary applications. Keep the generic callable interface authoritative and parity-tested.

### P1 investigation: repeated named Python method dispatch in protocol hot-adjacent paths

**Evidence.** `server_http2.c:174-192` calls `future.done()` and `future.set_result(...)` by string for waiter completion. HTTP/3 similarly calls `create_future`, `set_result`, task `exception`, and waiter `set_result` by string. HTTP/1 has named calls for waiter completion, timer cancellation, transport extras, and error reporting.

**Potential impact.** Attribute lookup, argument construction, and temporary reference churn per completion. It matters only where the call occurs per chunk or flow-control wakeup; transport close and error reporting are cold.

**Self-review.** A broad “replace every `PyObject_CallMethod`” edit would add cached bound-method ownership and invalidation complexity. Many sites are connection setup/teardown or errors and should be left alone. The native lint's NC005 correctly focuses on named dispatch inside loops; it is currently clean.

**Investigation.** Instrument counts by call site in the chunk-size benchmark. Microbenchmark only the completion helper variants, then ablate the helper in a full request. Compare current named dispatch with cached interned-name APIs and, where ownership is stable, cached bound callables. Reject changes that do not clear the end-to-end noise floor.

**Candidate resolution.** Cache only stable connection-owned callables such as loop `create_future`/`create_task`, as HTTP/2 already partly does. For arbitrary future instances, prefer CPython APIs that avoid format parsing/name allocation without retaining bound methods per future. Do not depend on undocumented asyncio object layout.

### P1 investigation: HTTP/2 receive-buffer compaction with a partial trailing frame

**Evidence.** `src/wreath/_native/server_http2.c:1694-1727` dispatches complete frames and then compacts every consumed prefix with `memmove`, preserving any partial trailing frame. Under a packet pattern containing complete frames plus a large partial next frame, the partial tail may be moved repeatedly.

**Potential impact.** Extra memory bandwidth proportional to callbacks times retained tail size. Frame-size bounds cap each move, so this is not the unbounded quadratic queue defect previously fixed.

**Self-review.** For a lone incomplete frame, the cursor remains zero and no move occurs. For aligned or batched complete frames, remaining length is zero. The suspicious case is narrower than “HTTP/2 parsing always copies.” Priority is below HTTP/3 pressure and Python orchestration.

**Investigation.** Build adversarial feeds that repeatedly leave 1/4, 1/2, and nearly one maximum-size frame after one complete frame. Add a test-only moved-byte counter and compare moved bytes to ingress bytes. Confirm with end-to-end fragmented TLS/plaintext feeds before changing layout.

**Candidate resolution.** Compact only when headroom is needed or the consumed prefix crosses a threshold, or use a ring/sliding buffer with bounded linearization at frame dispatch. Keep payload pointers valid through dispatch and preserve frame-size/error behavior.

### P1 investigation: GIL-held bulk work and scheduler fairness

**Evidence.** No `Py_BEGIN_ALLOW_THREADS` regions were found in the reviewed native sources. That is expected for code dominated by Python C API calls, but several operations can process large byte ranges: JSON encode/decode, template rendering, WebSocket masking, HPACK/Huffman work, compression, HTTP parsing, and PostgreSQL decode/hydration.

**Potential impact.** One large operation can monopolize the interpreter thread and inflate unrelated request latency. Under free-threaded CPython, extension-level synchronization and object access constraints remain separate concerns.

**Self-review.** Releasing the GIL around short work usually loses. Many loops touch Python objects and cannot safely release it without a two-phase representation. Single-request throughput may regress even when fairness improves. Existing native GIL/memory/error lint policy must remain clean.

**Investigation.** Separate two effects. First, run a large operation with a heartbeat and small requests on the same event loop to measure how long the synchronous call prevents the loop from regaining control; releasing the GIL alone cannot improve this because the event-loop thread is still inside C. Second, run the operation in one Python thread with a calibrated observer on another thread to measure actual GIL contention. Sweep payload size in both harnesses under normal, free-threaded, and optional-JIT modes.

**Candidate resolution.** For cross-thread contention, use a two-phase design: acquire/export immutable buffers and allocate output while holding the GIL, run a Python-object-free kernel without it above a measured threshold, then construct/commit Python results after reacquisition. For same-loop stalls, return to the loop through bounded chunking or explicit worker offload instead. Keep owners and buffer exports alive and make cancellation semantics explicit.

### P2 investigation: allocation-heavy protocol boundary construction

**Evidence.** Native protocols allocate scope/message dictionaries, tuples, lists, Unicode/bytes values, and task/future objects. `WreathRequestContext` can lazily expose a scope, but `Request` properties and generic ASGI still require Python objects. Header lists and response header normalization also create per-request pairs.

**Potential impact.** Small-request allocation rate, allocator contention, and cache pressure.

**Self-review.** Most objects escape into application code and cannot be safely arena-reused. Immortal/global caching is appropriate only for immutable constants. The code already caches many fixed keys and strings; the native lint's constant-table allocation rule is clean.

**Investigation.** Add allocation counts by lifecycle phase to the request decomposition harness. Separate unavoidable escaped objects from temporary internal objects. Prioritize objects allocated for every small static response and never observed by user code.

**Candidate resolution.** Cache immutable constants, use exact-size containers where sizes are known, and bypass scope/message materialization only through the negotiated native context. Do not introduce freelists until peak retention, interpreter finalization, subinterpreter, and free-threaded behavior are tested.

### P2 investigation: PostgreSQL Python-object assembly after native decode

**Evidence.** The PostgreSQL backend is broadly native, with geometric buffers, slabs, field tapes, native records, model layouts, and hydration plans. It still constructs Python lists/dicts/tuples and performs identity-map/model bookkeeping. `src/wreath/orm/session.py` remains a roughly 36 KiB Python unit-of-work coordinator; joined loads explicitly fall back through the `Record` path.

**Potential impact.** Object-heavy ORM reads, joined hydration, identity-map lookups, flush ordering, and Python unit-of-work frames can dominate after wire decode has been accelerated.

**Self-review.** This is not evidence that “the PostgreSQL driver should be rewritten in C”; it largely already is. ORM operations are application-visible and pointer-rich. Previous retained benchmarks show that bookkeeping shape matters more than indiscriminate translation.

**Investigation.** Use `wreath-decomp` and `bench_orm_shape.py`/PostgreSQL decode and flush benchmarks to split wire parse, scalar decode, record assembly, direct model hydration, identity merge, relationship assembly, and unit-of-work scheduling. Sweep rows, columns, null density, relation fan-out, and already-present identity-map fraction.

**Candidate resolution.** Extend native hydrate plans to joined-load assembly only if it removes a measured intermediate `Record` cost. Keep transaction/session control in Python unless its own ablation clears noise. Continue startup compilation and cache immutable plans, not results or model instances.

## Large Python surfaces without full C equivalents

“Large” here means substantial implementation surface, not automatically a performance problem. Some are cold control-plane code and should remain Python.

| Python surface | Approximate size | Native coverage today | Native-path relevance | Priority |
| --- | ---: | --- | --- | --- |
| `app.py` | 41 KiB | Native routers/policy primitives; no full lifecycle dispatcher | Every Wreath HTTP request, including pre-activation control flow | Highest; measure by stage |
| `binding.py` | 38 KiB | Native validation/parser helpers, but dependency resolution and argument assembly remain Python | Handler activation; shape-dependent | High for bound endpoints |
| `request.py` | 14 KiB | Native header/cookie/multipart helpers and `WreathRequestContext`; wrapper/state/body/form orchestration remains Python | Every request, with lazy feature costs | High structurally |
| `middleware/` | about 59 KiB total | Many native primitives (`TrustedNetworks`, token bucket, CSRF/web-policy helpers, compression); tape and hooks remain Python | Realistic protected request path | Highest cumulative question |
| `response.py` | 16 KiB | Native serialization/emission exists in server path; response classes/coercion remain Python | Every response; more for streaming/files/background | Medium/high |
| `http_client.py` | 33 KiB | `_client` accelerates only request serialization and response-head parsing | Outbound requests; pool, DNS, retries, redirects, TLS, body I/O in Python | High for client-heavy apps, not inbound baseline |
| `orm/session.py` | 36 KiB | Native driver, records, model storage, hydration/compiler pieces | Database-backed handlers | High, but benchmark per ORM phase |
| ORM compiler/registry/model/constraints | over 100 KiB combined | Shape keys, value collection, storage and hydration partly native | Mostly startup/cold compilation; some query construction | Low unless decomposition says otherwise |
| `webhooks.py` | 51 KiB | Uses native HTTP codec/client pieces indirectly; delivery/outbox/retry orchestration is Python | Feature workload, not base request path | Keep Python until feature benchmark identifies a kernel |
| `server.py` | 34 KiB | HTTP/1 and HTTP/2 protocols are native; Python owns selection/configuration/TLS and fallback paths | Mostly startup/connection control; some negotiated protocol glue | Medium, inspect by protocol |
| `_pure/server.py` and `_pure/postgres.py` | about 51/58 KiB | Full native equivalents exist for selected deployments | Not active when native backend is selected; parity references | Do not optimize for native path |
| CLI/devtools/typegen/OpenAPI | large in aggregate | Selective native rendering only | Cold/control plane | No native rewrite without separate evidence |

The notable gaps are therefore not all large files. The request-critical gaps are the **application lifecycle, middleware tape, request wrapper/state, binding activation, and ORM session coordination**. Conversely, `webhooks.py`, CLI, OpenAPI, introspection, migration planning, and most registry compilation are large but are not justification for C equivalents by size alone.

## Rejected or downgraded suspicions

The self-review rejected the following broad claims:

- **“The native queues are quadratic.”** Current HTTP/2, HTTP/3, and PostgreSQL queues use head indices and batched compaction. The native lint confirms no known front-deletion pattern.
- **“Buffer growth is additive.”** Reviewed dynamic buffers generally grow geometrically with overflow checks. No NC003 finding is present.
- **“HTTP/1 slow input rescans from byte zero.”** Existing scan cursors address the known incremental-parser issue; regressions should remain covered, but this is not a new finding.
- **“HTTP/3 buffers the whole response before sending.”** The file header is stale. Headers/data-reader are submitted when response start arrives. The actual concern is unbounded retained unacknowledged chunks and immediate send completion.
- **“Every named method call should be cached.”** Most named calls are cold or instance-specific. Only per-body/per-wakeup sites deserve measurement.
- **“No GIL release is inherently slow.”** It is a fairness/scaling hypothesis for large kernels, not a defect on small hot paths.
- **“Every large Python module needs a C twin.”** Control-plane and policy-rich code is safer and often cheaper to keep in Python. Translate measured kernels or fuse proven lifecycle stages, not file counts.

## Ordered investigation program

### Phase 0 — preserve baselines

1. Record environment, Python build mode, compiler flags, native module paths/timestamps, server, loop, CPU governor, and dependency groups.
2. Retain untouched request trace, tape decomposition, lifecycle decomposition, native pressure, HTTP/1 storage, and ORM baselines.
3. Record at least one A/A pair for each benchmark family. Do not attribute deltas below its own noise floor.
4. Keep `uv run wreath-native-lint`, memory/error/GIL/boundary lints, and request-boundary baseline clean.

### Phase 1 — pressure and shape diagnostics

1. Add HTTP/3 blocked-send retained-byte instrumentation and benchmark.
2. Add HTTP/1/2/3 fixed-total-byte chunk-size sweeps.
3. Add HTTP/2 moved-byte instrumentation for adversarial partial tails.
4. Add lifecycle ablations for pre-activation stages and middleware groups.
5. Add allocation counts and event-loop heartbeat latency to the shared measurement harness where observer cost is calibrated.

Instrumentation must be test/benchmark-only or compile-time gated. Counters must not remain on the production hot path unless their cost is independently priced.

### Phase 2 — choose at most one fix per evidence set

Decision gates:

- Implement HTTP/3 high/low-water send backpressure if retained bytes scale with produced bytes under blocked flow control.
- Implement a private native request-context body path if chunk-object overhead clears noise and generic ASGI parity can remain exact.
- Compile/fuse lifecycle stages only if a cumulative ablation wins repeatedly; do not port isolated sub-microsecond frames.
- Change HTTP/2 compaction only if moved-byte amplification is observed in realistic fragmentation.
- Release the GIL only above measured thresholds that improve fairness or scaling without unacceptable throughput regression.

Each fix gets focused red tests first, its own retained after-results, and a separate review unit when ownership/backpressure semantics differ.

### Phase 3 — verification

For every accepted change:

- native and `WREATH_PURE=1` behavioral parity where a pure twin exists;
- generic ASGI server compatibility and Wreath native-server fast-path tests;
- cancellation, disconnect, reset, exception, and finalizer ordering;
- HTTP/2/3 flow-control and retransmission lifetime tests;
- sanitizer runs for changed C ownership;
- default and full marked test suites, Ruff, ty, strict docs, native lints, and request-boundary check;
- repeated before/after benchmark results with every trial retained.

Report confirmed wins, regressions, neutral results, and unresolved below-noise hypotheses separately.

## Completion criteria

This audit is resolved when:

1. HTTP/3 blocked-peer memory is measured and either bounded with tested backpressure or documented as an explicit, quantified trade-off.
2. Pre-activation Python lifecycle cost is decomposed by stage with an A/A floor; any native fusion is based on cumulative measured cost.
3. Body chunk object/future overhead and HTTP/2 compaction amplification are quantified rather than inferred.
4. Large Python surfaces are classified as hot data plane, workload-specific, or cold control plane, with no C rewrite proposed solely because of source size.
5. Accepted changes preserve ASGI semantics, pure/native parity, cancellation and ownership safety, and reproducible evidence.

Until those measurements exist, the only immediate source change justified by this audit is correcting the stale top-of-file HTTP/3 buffering comment. Even that should land separately from performance work so documentation cleanup cannot be mistaken for a measured optimization.
