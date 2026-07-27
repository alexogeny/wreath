# Prescriptive plan: profile-guided hotspot remediation

Status: ready for test-first implementation

Related material:

- `AGENTS.md`
- `docs/agents/index.md`
- `docs/agents/manifest.json`
- `docs/plans/native-c-orm.md`
- `docs/plans/native-c-hotspots.md`
- `docs/plans/native-c-http1-routing-storage-pressure.md`
- `docs/plans/single-pass-request-pipeline.md`
- `benchmarks/README.md`

## Goal

Remove the confirmed quadratic ORM session operations, reduce measured Python-object churn in the native HTTP/1 request/response path, bound multipart memory amplification, and close the native error-handling findings discovered by the July 2026 profile. Preserve ASGI, ORM ordering, native/pure parity, and public request/response behavior. Every performance change must retain reproducible before/after artifacts; this plan does not treat one profile or one timing run as proof of an improvement.

## Measured baseline and scope

The profile used CPython 3.14.6 on Linux x86-64 with the repository's optimized native extensions. Retain a fresh baseline before implementation rather than treating these diagnostic runs as publishable results.

Confirmed or measured weak spots:

- `src/neo/orm/session.py:597-599` calls `self._new.index(item)` from a sort key. Key extraction is O(n²), and `_order()` also scans `Registry.specs` for every object.
- `Session.add()` and `Session.delete()` use linear list membership/removal for pending objects, creating quadratic behavior across large units of work.
- Perf attributed HTTP/1 bridge CPU to `_Py_Dealloc` (3.11%), `begin_response` (1.96%), `PyBytes_FromStringAndSize` (1.72%), `PyObject_GC_Del` (1.40%), and `find_sub_from` (1.04%).
- Cachegrind attributed 12.0% of instructions and 13.6% of conditional branch mispredicts to `dictobject.c`; `_Py_dict_lookup` alone used 6.7% of instructions.
- `neo-native-boundary-lint` reports 39 native-boundary findings. The measured HTTP/1 path includes repeated dictionary/object operations in `server_http1.c`; decision routing has loop and lookup findings in `dtrouter.c`.
- Multipart parsing at `src/neo/_native/multipart.c:155-160` copies each part while `Request.body()` retains the complete body. A 16 MiB multipart workload copied 16 MiB of part data, peaked near 75 MiB RSS, and took 2.69 times the 8 MiB case.
- Empty WebSocket continuation fragments retained only 223 bytes after 20,000 fragments, but 10,000 to 20,000 fragments scaled by 2.32 times. This is a CPU-amplification follow-up, not a leak.
- `neo-native-error-lint` reports ignored integer-status Python API results in `http3_asgi.c:630`, `http3_asgi.c:813`, and `server_http2.c:745`.

The following are not primary rewrite targets in this plan:

- HTTP/1 receive queue, PostgreSQL retired slabs, cookie reuse, H2 flush, and normal router compilation measured approximately linearly.
- High-cardinality JSON keys were 1.99 times slower than stable keys, but the prior JSON cache was intentionally removed to avoid hidden retention. Do not restore unbounded key ownership. Revisit JSON only after the HTTP/1 allocation work is measured.
- The complete-body multipart API continues returning `bytes`. Do not silently change `UploadedFile.data`, `Part.data`, or `Request.body()` to `memoryview`.

## Repository constraints

- Target CPython 3.14 and keep `src/neo` free of mandatory third-party dependencies.
- Preserve native/pure observable parity; `NEO_PURE=1` remains a correctness path.
- Preserve deterministic ORM insertion order, model dependency order, update/delete ordering, rollback behavior, ownership checks, and identity-map semantics.
- Preserve ASGI message dictionaries and arbitrary conforming user applications. Fast paths may specialize exact built-in values only when a generic fallback remains correct.
- Keep multipart and request-body ownership explicit. Limits must be enforced while receiving, before an oversized body is fully joined or multipart parts are copied.
- Keep native lint rules strict. An intentional boundary finding needs an in-place waiver with a bounded reason; do not weaken a repository-wide rule.
- Run native sanitizers after C changes and retain raw benchmark trials, environment metadata, errors, median, p95, and memory measurements.

## 1. Make ORM unit-of-work bookkeeping linear

Files:

```text
src/neo/orm/registry.py
src/neo/orm/session.py
tests/orm/test_session.py
benchmarks/postgres/bench_orm_flush.py
benchmarks/postgres/README.md
```

Compile model order once in `Registry` as a private `dict[type[Model], int]`. Populate it when `specs` is frozen and make `Session._order()` an O(1) lookup. Keep `Registry.specs` as the public deterministic tuple.

Replace pending-list scans with explicit ordered membership:

- keep `_new` and `_deleted` as ordered lists because flush order is observable;
- add private identity-based membership sets keyed by object identity, not model equality;
- assign each newly scheduled object a monotonic insertion ordinal once;
- remove the ordinal and membership entry when a transient object is unscheduled, after successful flush, and on every cleanup path;
- sort inserts by `(compiled_model_order, insertion_ordinal)` without `list.index()`;
- do not use model `__eq__` or `__hash__` for session ownership bookkeeping.

A small private helper should own schedule/unschedule/clear invariants so `_new`, `_deleted`, their membership sets, and ordinals cannot diverge. If an insert fails, preserve the current retry/rollback state rather than partially clearing bookkeeping.

Add focused tests that prove:

- interleaved model instances flush in model order and preserve insertion order within one model;
- adding the same object twice schedules it once;
- deleting a transient object removes every bookkeeping entry and permits it to be added again;
- successful flush and close leave no stale strong references;
- failure and rollback preserve the current documented retry semantics;
- objects with pathological equality/hash methods do not affect identity ownership.

Add `benchmarks/postgres/bench_orm_flush.py` using the existing fake database seam from `tests/orm/conftest.py`; it must isolate bookkeeping from network and SQL latency. Measure 1,000, 2,000, 5,000, and 10,000 pending instances with repeated trials and report add, unschedule, ordering, and bookkeeping-only flush preparation separately. Add a test-only operation counter for membership probes/order lookups so pytest proves linear work without wall-clock assertions.

Acceptance gate: doubling pending instances must not produce a quadratic operation count. Timing should remain near-linear across the two largest sizes; report the ratio without treating noisy timing alone as a failure.

## 2. Reduce measured HTTP/1 Python-object churn

Files:

```text
src/neo/_native/server_http1.c
src/neo/_native/server_request.c
src/neo/_native/server_common.c
src/neo/_native/dtrouter.c
src/neo/_devtools/native_boundary_lint.py
tests/test_native_boundary_lint.py
tests/server/test_native_http1.py
tests/test_request_pipeline.py
benchmarks/bench_native_request_bridge.py
benchmarks/bench_request_pipeline.py
```

Work from profiles, not the count of lint findings. First retain matching `perf`, counters, Cachegrind, and request-bridge baselines. Use the exact same workload and CPU placement for after-runs.

For `begin_response`, HTTP receive, and WebSocket send/receive:

- hoist stable interned keys and exact-type checks to module initialization where not already cached;
- avoid materializing temporary sequences when exact `list`/`tuple` headers can be iterated safely in place;
- use borrowed references while the owning message remains alive and keep a generic iterable fallback for conforming ASGI applications;
- do not replace required ASGI dictionaries with private public-facing message types;
- avoid creating empty body/header containers when an existing immutable singleton or owned empty value is valid;
- keep error checks after every Python C API call.

For decision routing, optimize only a function that appears in a matching profile. Prefer native segment metadata and cached stable objects over repeated Unicode/PyLong construction. Preserve route specificity, registration order tie-breaking, HEAD fallback, authorization tickets, and path-parameter ownership. Do not combine this work with a new routing algorithm.

Add focused native-boundary lint fixtures before changing production C. The target is not necessarily zero findings across all native code: measured hot functions must either become clean or carry narrow reasoned waivers showing why the remaining Python operation is required and bounded.

Performance gates:

- Public static routing must not regress by more than 3% in repeated isolated trials. The diagnostic classified-public path was about 8% slower than the legacy public path and should receive a direct eligible-public fast path if the profile confirms classification overhead.
- Missing and protected-route improvements from single-pass classification must remain within 5% of their retained baseline.
- Empty-body native bridge throughput must improve in the median without worsening p95 or increasing peak RSS; require at least nine measured trials before claiming a win.
- Compare perf symbols and Cachegrind instruction/branch totals. A patch that merely moves cost between Python allocation/deallocation symbols is not accepted.

## 3. Bound multipart memory before adding a streaming API

Files:

```text
src/neo/request.py
src/neo/multipart.py
src/neo/_pure/multipart.py
src/neo/_native/multipart.c
src/neo/app.py
tests/test_request.py
tests/test_client_sessions_forms.py
tests/test_native_parity.py
benchmarks/bench_native_http1_storage.py
docs/guides/requests.md
docs/reference/http.md
docs/cookbook/uploads.md
```

Keep the current complete-body parser compatible, but make its worst-case ownership bounded and visible:

- add an application/request configuration for maximum buffered request-body bytes, maximum multipart parts, maximum header bytes per part, and maximum in-memory uploaded-file bytes;
- enforce the body limit incrementally in `Request.body()` before appending a chunk or joining accumulated chunks, including portable ASGI servers;
- enforce part/header/count limits in both native and pure multipart parsers with identical exception types and boundary behavior;
- check integer overflow before every length addition in C;
- fail before constructing a part `bytes` object that would exceed the configured in-memory form budget;
- clear partially built native/Python part containers on every error path.

Use existing application configuration/compiled request construction rather than process-global settings. Defaults must be conservative but compatibility-aware; document them as a pre-1.0 API decision. A route-specific override may be added only through existing route metadata compilation, not request-time global mutation.

Do not claim that limits remove copy amplification. After limits are stable, open a separate API plan for streamed/spooled uploads if applications need files larger than the in-memory budget. That API must not overload `Request.form()` with values whose lifetime or type differs silently from `UploadedFile.data: bytes`.

Extend `multipart-peak` with exact-limit and one-byte-over cases, multiple concurrent request objects, and separate retained/peak memory. Acceptance requires bounded failure before a second full-size part copy, native/pure parity, and no increase in retained memory after failed parses.

## 4. Bound empty-fragment CPU amplification

Files:

```text
src/neo/_native/server_http1.c
src/neo/_native/server_http2.c
src/neo/_native/http3_asgi.c
tests/test_websocket.py
tests/http2/test_asgi.py
tests/http3/test_asgi.py
benchmarks/bench_native_http1_storage.py
```

Retain the existing message-byte limit and negligible-retention behavior. Add a per-message fragment-count limit shared by HTTP/1, HTTP/2, and HTTP/3 WebSocket implementations where supported. Empty fragments count toward the limit. Reject excess fragmentation with close code 1009 and clear state on close, protocol error, cancellation, and connection loss.

Before changing accumulator representation, profile the 10,000/20,000-fragment cases to distinguish parser dispatch, masking, UTF-8 state, and buffer growth. Optimize only the dominant operation. Tests use counters to prove one bounded pass per frame; benchmarks retain timing and `tracemalloc` evidence.

Acceptance requires approximately linear operation counts, retained bytes remaining O(1) for empty fragments, and unchanged behavior for valid fragmented text/binary messages.

## 5. Make native error handling clean before performance refactors

Files:

```text
src/neo/_native/http3_asgi.c
src/neo/_native/server_http2.c
tests/test_native_error_lint.py
tests/http2/
tests/http3/
```

Add failing tests for each ignored integer-status operation, then check `< 0` and propagate the active exception through the function's existing cleanup label. Exercise allocation/mutation failure where the test harness permits fault injection. Run `neo-native-error-lint` after each C review unit; the acceptance state is zero findings with no bare waiver.

Do this before nearby object-lifetime optimizations so profiles are collected from code that cannot return success with an exception set or continue with partial state.

## Correctness rules

- ORM scheduling is by object identity; model equality cannot merge distinct rows.
- Insert order remains deterministic across native and pure ORM modes.
- A failed flush does not silently lose pending work or leak a leased connection.
- ASGI receive/send ordering, disconnect behavior, response framing, header validation, and backpressure remain unchanged.
- Native fast paths always retain a generic conforming-ASGI fallback.
- Multipart limits apply before allocation/copy and cannot be bypassed by chunk boundaries, missing `Content-Length`, many empty parts, or pure mode.
- WebSocket fragment limits apply to empty and non-empty fragments and release all accumulator state on failure.
- No unbounded process-global cache, intern table, model registry, or request state is introduced.
- No performance claim is made from a single run, concurrent benchmark runs, or profiler-instrumented throughput.

## Files touched

Expected primary paths:

```text
src/neo/orm/registry.py
src/neo/orm/session.py
src/neo/request.py
src/neo/multipart.py
src/neo/_pure/multipart.py
src/neo/_native/multipart.c
src/neo/_native/server_http1.c
src/neo/_native/server_http2.c
src/neo/_native/http3_asgi.c
src/neo/_native/dtrouter.c
tests/orm/test_session.py
tests/test_native_boundary_lint.py
tests/test_native_error_lint.py
benchmarks/postgres/bench_orm_flush.py
benchmarks/bench_native_request_bridge.py
benchmarks/bench_native_http1_storage.py
benchmarks/README.md
docs/guides/requests.md
docs/reference/http.md
docs/cookbook/uploads.md
```

Update `docs/agents/manifest.json` only if source/test routing or public request configuration changes enough that the existing subsystem map becomes inaccurate.

## Verification

Run focused checks after each review unit, then the broader suite:

```bash
uv run pytest tests/orm/test_session.py
uv run pytest tests/test_native_boundary_lint.py tests/test_native_error_lint.py
uv run pytest tests/test_request.py tests/test_client_sessions_forms.py tests/test_native_parity.py
uv run pytest tests/test_websocket.py tests/http2/test_asgi.py tests/http3/test_asgi.py
uv run neo-native-lint
uv run neo-native-boundary-lint
uv run neo-native-error-lint
uv run neo-native-memory-lint
uv run neo-native-gil-lint
uv run ruff check .
uv run ty check
uv run pytest
uv run pytest -m '' -n 4
uv run --group docs mkdocs build --strict
```

Run the server sanitizer suite after server/multipart C changes and the relevant PostgreSQL/native suite if shared native utilities change. Retain sanitizer commands and logs in the implementation report.

Retain before/after benchmark and profile artifacts under distinct paths, including:

```text
benchmark-results-orm/hotspot-before.json
benchmark-results-orm/hotspot-after.json
benchmark-results-native-http1-storage/hotspot-before.json
benchmark-results-native-http1-storage/hotspot-after.json
.profiles/hotspot-before/
.profiles/hotspot-after/
```

Record Python version, platform, compiler flags, native module paths, workload sizes, warmups, every measured trial, median, p95, errors, peak RSS, and profiler command. Run comparison trials serially on an otherwise idle machine.

## Acceptance checks

- Scheduling and preparing 10,000 new ORM instances performs linear membership/order work; no `list.index()` or repeated registry scan remains in the flush key.
- ORM insertion/deletion order, rollback, ownership, and identity-map tests remain unchanged and pass in native and pure modes.
- Measured HTTP/1 changes reduce allocation/deallocation or dictionary work without regressing public static routing beyond 3%, protected/missing routing beyond 5%, p95, errors, or peak RSS.
- Oversized portable-ASGI bodies and multipart forms fail before full buffering/part copying; limits behave identically in native and pure parsers.
- Valid multipart forms still expose `UploadedFile.data` and `Part.data` as exact `bytes`.
- Empty WebSocket fragment work has linear operation counts, bounded retained memory, and deterministic 1009 failure over the configured fragment limit.
- `neo-native-error-lint`, `neo-native-memory-lint`, `neo-native-gil-lint`, and `neo-native-lint` report zero findings. Remaining boundary findings are outside measured hot paths or have narrow justified waivers.
- Focused tests, the default suite, full marked suite, sanitizers, Ruff, ty, and strict docs build pass.
- Raw before/after evidence is retained, and the implementation report distinguishes confirmed wins, neutral changes, regressions, and unresolved profile noise.
