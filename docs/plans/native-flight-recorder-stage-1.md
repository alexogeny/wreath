# Native Flight Recorder plan — Stage 1: architecture and native spine

**Status:** the native spine described here is **implemented and shipped** (Off +
Pulse). See ADRs 0021/0022 for the decisions taken. The completion checklist
below tracks what has landed against this plan; Stage 3 carries the stage-by-stage
checklist for the whole programme.

## Implementation checklist (Stage 1 spine)

- [x] Separate `wreath._native._flight` extension with a versioned capsule API
      (`flight.c`/`flight.h`/`_flightmodule.c`); absent/disabled ⇒ static no-op.
- [x] Fixed 64-byte cell schema, C mirror + Python twin, parity-tested
      (`flight_schema.h` ↔ `_flight_schema.py`).
- [x] `wreath_nfr_context`, worker state, single-writer SPSC ring, log2
      histograms, free-list active table with seqlock generations.
- [x] Bounded loss accounting (one categorized counter per drop).
- [x] Off is a single not-taken branch on every hook; Pulse commits ≤1 completion
      cell per request.
- [x] HTTP/1 ingress/completion/abandon hooks.
- [x] HTTP/2 per-stream ingress/completion/abandon hooks.
- [x] HTTP/3 per-stream ingress/completion/abandon hooks (mirrors H2; `_http3`
      resolves the capsule itself). *(landed 2026-07-18)*
- [x] `bytes_in` / `bytes_out` wire accounting on the completion cell for
      HTTP/1, HTTP/2, and HTTP/3. *(landed 2026-07-18)*
- [x] Strict W3C `traceparent` parse in C + paired correlation cell.
- [x] Acceptance microbenchmark `benchmarks/bench_flight_recorder.py`: pinned A/A
      noise 0.3–0.5%, Pulse+completion ≈ +8 ns/request over Off in isolation.
      *(landed 2026-07-18)*
- [x] Hot-path rice: histogram bucket via `clz` (was a shift loop); completion
      cell fill skips a per-request `memset`. *(landed 2026-07-18)*
- [x] WebSocket completion cells. One cell per WebSocket *session* (protocol
      WEBSOCKET), started at the handshake in `begin_websocket` and emitted in the
      `ws_mode` branch of `apply_app_outcome`. `status` carries the handshake
      disposition (101 established, 403 rejected, 500 pre-accept error);
      `terminal` carries how the session ended (OK / ERROR / DISCONNECTED /
      CANCELLED); `bytes_in` accumulates received frame payloads, `bytes_out`
      every byte written. Incoming `traceparent` still produces a correlation
      cell. Route attribution works too: WEBSOCKET routes are in the metadata
      image (plan_id 0 — WS handlers have no HTTP plan) and `_handle_websocket`
      stamps the matched IDs into the retained scope for C to read at completion.
      *(landed 2026-07-18)*

**Status (historical):** planning document only; no runtime implementation is included.

**Project name:** Wreath. Historical documents and some repository metadata still say Neo; this plan uses current `wreath.*`, `WREATH_*`, and `src/wreath/` names.

This is part 1 of 3. Part 2 specifies Inspector, OpenTelemetry, recording, replay, security, and configuration. Part 3 provides the implementation sequence, files, test matrix, risks, and non-goals.

## Goal

Build one optional native subsystem, provisionally **Native Flight Recorder (NFR)**, underneath the reserved modules `wreath.telemetry`, `wreath.inspector`, `wreath.recording`, and `wreath.replay`. NFR owns one compact execution context, bounded worker state, fixed event schema, and versioned metadata/recording model. The four Python modules are configuration, control, projection, and inspection views over that one model; they must not become separate event pipelines.

The smallest repository-compatible boundary is a separate optional C extension, `wreath._native._flight`, exposing a versioned C capsule API to `_server`, `_core` routing backends, `_client`, and `_postgres`. Do not put it in `_core`: ADR 0008 and the current build deliberately keep framework accelerators, server, client, PostgreSQL, and HTTP/3 independently importable. If `_flight` is absent or disabled, callers retain a static no-op path and existing behavior.

A **worker** initially means one `wreath.server.Server` in one process and one event-loop thread. Wreath has no multi-process worker supervisor today; this plan does not invent one. Each worker owns one recorder, active-request table, counters/histograms, single-writer ring, and bounded capture pool.

The first guarantee is narrow: Wreath's native server can emit a compact completion record without adding Python work to a Pulse request. Full framework phase detail is opt-in because the current application pipeline in `app.py` is Python. The plan does not claim that Wreath's present whole request lifecycle is C-only.

## Repo conventions and constraints

- Target CPython 3.14 and preserve ASGI semantics.
- Keep `src/wreath` free of mandatory third-party runtime dependencies.
- Keep framework and server extensions independently importable.
- Use Python for startup compilation, configuration, control, optional extension, and inspection.
- Do not add request-path Python calls, allocation, locks, syscalls, formatting, protobuf, network I/O, or exporter interaction.
- Reuse protocol, routing, request, endpoint compilation, client, PostgreSQL, and ORM state rather than mirroring it.
- Keep all tables, rings, queues, and capture pools bounded; overflow drops telemetry and increments a categorized loss counter.
- Default Pulse instrumentation emits at most one completion cell per request. Detailed phase events are armed/sampled/promoted only.
- Preserve the existing noise-aware benchmark method in `src/wreath/_devtools/measure.py`; use ablation, not cProfile.

## 1. Current integration points

### Reserved surfaces and existing observability

- `src/wreath/telemetry.py`, `inspector.py`, `recording.py`, and `replay.py` are intentional empty scaffolds. `docs/reference/roadmap.md` reserves exactly these imports.
- `src/wreath/_native/observability.c` already supplies strict request-ID validation and `Server-Timing` formatting through `_core`. `middleware/request_id.py` and `middleware/timing.py` define current correlation and monotonic timing behavior. Reuse their semantics, but do not route NFR through middleware.
- `docs/plans/future/05-operational-observability.md` already calls for bounded labels, strict W3C propagation, optional exporters, worker-local native counters/histograms, a fixed ring, and explicit loss accounting.
- `docs/plans/future/14-control-system-testing.md` proposes virtual clocks and scripted peers. Replay should consume those concepts rather than create another fault/time vocabulary.
- `docs/cookbook/agents/documenting-a-module.md` defines the guide/reference/nav/`llms.txt` work needed when each scaffold becomes public.

### Application, routing, and endpoint compilation

- `Wreath.__call__`, `_wreath_http`, `_handle_http`, `_finish_http`, and `_compile_routes` in `src/wreath/app.py` are the application lifecycle seams.
- `_compile_routes()` already does startup work once: merge auth requirements, compile capabilities, call `compile_binder()`, fuse route middleware, and populate the router. This is where stable route, endpoint-plan, dependency, policy, serializer, and limit IDs should be assigned and metadata published.
- `router.RouteDefinition` already contains path, methods, endpoint, route middleware, tags, dependencies, auth requirement, and operation ID.
- The executable “endpoint plan” is not a first-class object today. `binding.BindingSpec` describes typed binding, while `compile_binder()` and middleware compilation return closures. NFR should add an immutable descriptor beside these callables, not replace their execution model.
- `_routing.Router` selects trie, decision-tree, or bitset tables. Native `DRoute` already stores handler, path shape, specificity, and authorization clauses. Add numeric metadata IDs beside those fields and preserve current `match()` return shapes and routing parity tests.
- Avoid an extra Pulse crossing by extending the existing internal native `match/classify/resolve` call with an optional execution-context handle. The C router writes `route_id`/`plan_id` into that context when it chooses a route. Public router methods remain unchanged.

### Request and server ownership

- `request.Request` is a small slotted Python object backed by an ASGI scope and already has explicit `_context` and `_state`. It can expose an NFR handle lazily without owning native recorder storage.
- HTTP/1 uses `WreathHttpProtocol` in `server.h`; it owns parsing, request framing, response status/framing, task completion, timers, WebSocket state, and the optimized `_wreath_http` entry. `server_request.c` already creates a lazy `_RequestContext` for Wreath applications.
- HTTP/2 has per-stream `Http2Stream` request/response, task, body, and flow-control state. HTTP/3 has analogous `WreathH3Stream` state. These are the natural owners of per-request context and scratch.
- HTTP/1 task completion/reset and HTTP/2/3 stream completion are centralized enough to commit a completion record before state is reset or freed.
- `server.Server` owns listeners, the active protocol set, lifespan, date timer, and shutdown. Recorder/projector/Inspector lifecycle belongs here—not in module globals.
- HTTP parsing, protocol state, and response encoding are native, but Wreath enters Python at `_wreath_http`; HTTP/2 and HTTP/3 currently call generic ASGI. Instrumentation must preserve that architecture.

### Connections, WebSockets, client, and database

- `Server._protocols` is already the control-plane connection registry. Native protocol/stream fields supply exact connection, WebSocket, response, and flow-control facts; Inspector should reference them rather than duplicate every field.
- `http_client.HTTPClient` owns bounded pool state and exposes `ClientSnapshot`; retry, acquire, DNS, connect, write, and response read seams are explicit. It is currently mainly Python despite `_client`. Instrument it only for armed Detailed/Forensic requests until a native client path exists.
- `postgres.Pool`, `Database`, and `Statement` own workload pools, leases, and calls; wire state/codecs live in `_native/_postgres`. Reuse connection/query state and statement IDs. Never copy SQL or database values into ordinary records.
- ORM registries, model fingerprints, sessions, and PostgreSQL plans already offer deterministic identities. Reference them instead of serializing descriptions per event.

### Build, tests, and benchmarks

- `setup.py` builds separate optional `_core`, `_server`, `_client`, `_postgres`, and opt-in `_http3` C11 extensions. Add `_flight` separately and resolve its capsule during sibling-module initialization.
- Pure/native twins and differential tests are established conventions. A bounded pure recorder should be the readable schema/overflow oracle, not a performance substitute for native Pulse.
- Protocol tests drive fake transports without sockets (`tests/http2/conftest.py` is the clearest example). Reuse this for exact event sequences and transport replay.
- `wreath-request-trace` must prove Off/Pulse add no Python/native crossings. Extend the existing benchmark runner and retained artifact conventions.

### Naming debt

`docs/agents/manifest.json`, older plans/ADRs, `mkdocs.yml` site/repository URLs, and `.pi/project.json` still contain Neo or unrelated stale project metadata. They are not a source for new names. Cleaning them is outside NFR except where an implementation touches a file.

## 2. Native data structures and ownership

Exact sizes are acceptance decisions after cache/benchmark measurement, not design assumptions.

### `wreath_nfr_context`

Embed directly in the active HTTP/1 request state and each HTTP/2/3 stream, or store in a preallocated worker slot referenced by a small index if inline cache cost proves material:

- 64-bit worker-local `request_id` and `connection_id`;
- 128-bit trace ID, 64-bit parent span ID, and 64-bit span ID;
- monotonic start timestamp and optional last-phase/deadline timestamp;
- 32-bit route ID, plan ID, and active-slot index;
- mode and flags: W3C sampled, detailed/forensic armed, error/slow promotion, propagation validity, body truncation, telemetry loss;
- current lifecycle phase and terminal status/error-class ID;
- explicit owner pointer/index to the worker recorder.

Off mode does not initialize context. Pulse initializes only correlation, active state, route attribution, and completion fields.

### `wreath_nfr_scratch`

Use a fixed array, initially budgeted at 8–12 16-byte phase cells. A cell contains event kind, component ID, flags, monotonic delta, and one compact value. Only an armed request writes it. Exhaustion increments request and worker loss counters; it never spills or allocates. Completion copies selected cells to the ring, then reclaims the request slot.

Do not retain body/header bytes in scratch. Error/latency triggers can preserve timings already present, but payload capture is possible only when a pre-request policy armed Forensic mode.

### Completion/event cells

Use a fixed 64-byte ring cell with schema version, kind, flags, request/trace key, IDs, timestamp/duration, and compact numeric payload. The default completion includes request/connection/route/plan IDs, trace/span correlation, duration, status, protocol, bytes in/out, and terminal flags. Detailed events reuse the same cell shape. If trace IDs do not fit cleanly, use a paired correlation cell—not variable-length records.

No strings, formatting, pointers, Python objects, or variable-length payloads enter the ring.

### Worker state

`wreath_nfr_worker` owns:

- immutable compiled mode/config and worker ID;
- cache-line-separated counters for requests, statuses, failures, protocol errors, active connections/requests/WebSockets, and every loss reason;
- fixed log2 or configured explicit-bucket histograms indexed by approved route/phase IDs; reject unbounded cardinality at startup;
- one SPSC ring: event-loop/server writer, projector reader, atomic head/tail with acquire/release ordering;
- fixed active-request slots with generation/seqlock fields for non-blocking Inspector snapshots;
- only the connection/WebSocket summaries not already cheap to obtain from protocol objects;
- a capture pool of fixed-size slabs and descriptors;
- a bounded command mailbox. Commands apply at event-loop safe points; request code never takes its lock.

The writer checks capacity once. If full, increment `ring_dropped` and return. Projector state never publishes backpressure into request state.

### Static metadata image

At application compilation, Python produces a canonical `MetadataImage` with numeric tables for routes, endpoint descriptors, dependencies, auth policies, middleware, serializers/validators, limits, HTTP clients, databases, ORM models/query shapes, and native components. Runtime records contain only IDs.

IDs must be deterministic within an application image: canonicalize stable descriptors and hash a versioned image. Recordings carry the image hash and, where portable, the image. Process-local addresses, `repr()`, and Python randomized hashes are forbidden. Changed metadata is an explicit replay compatibility decision.

### Extension boundary

`wreath._native._flight` exports a named versioned capsule such as `wreath._flight._C_API.v1` with no-op-safe functions for context start/end, route attribution, phase mark, counters/histograms, capture append, and metadata/config publication. Sibling extensions resolve it once at initialization or recorder attachment. No request-time import, attribute lookup, capsule lookup, or Python call is allowed.

Recorder objects own native memory and are explicitly attached to `Server`. Dependants hold a validated C pointer plus Python lifetime ownership outside hot operations. Free-threaded builds use C atomics for reader/writer publication; event-loop ownership remains the single-writer invariant.

## 3. Request lifecycle instrumentation

### Pulse/native points

1. **Connection accepted/closed:** assign a connection ID; update counters and active connection state.
2. **Header complete / stream created:** parse propagation, apply compiled mode/trigger policy, reserve an active slot, initialize context.
3. **Route selected:** the existing native router call writes route/plan IDs; misses use reserved IDs.
4. **Response start:** save status and response mode using existing protocol state.
5. **Body ingress/egress:** reuse existing byte counts; emit no per-chunk Pulse event.
6. **Task/stream completion:** compute duration, update counters/histograms, publish at most one completion cell, release active slot.
7. **Protocol failure/disconnect/timeout:** set terminal flags before normal cleanup; do not create a separate unbounded error log.

### Detailed-only points

Ingress complete, classification, authentication, authorization, route middleware, binding/validation, database acquire/query/transaction, outbound DNS/connect/TLS/pool wait/request/retry, handler activation/return, serialization, first byte, stream completion, and background handoff.

Native phases mark directly. Current Python application/client phases call one small native marker only when armed. Pulse must not call it, so its request-boundary baseline remains unchanged. Any Detailed crossing growth is documented as opt-in.

### Trigger evaluation

Compile predicates into cheap ordered tests:

- before request: explicit capture token, trace ID/prefix, path-to-route rule, deterministic sampling, propagated sampled flag;
- during request: native component, protocol, or dependency failure;
- completion: status set/range and latency threshold.

Latency/error/status triggers can promote fixed timing scratch and completion. They cannot recover uncaptured bodies or external values. Route-specific forensic policy is compiled into the plan and starts after route selection; earlier ingress bytes require an explicit ingress-prefix policy.

## 4. Threading and worker model

- One `Server`/event loop is one recorder writer. HTTP/1 protocols and HTTP/2/3 streams on that loop share it.
- The projector runs in a dedicated control-plane thread or helper process. It drains native cells into a second bounded export queue. Export, DNS, TLS, protobuf, Python, SDK callbacks, and retries occur there only.
- If projector/export stalls, the ring fills and new telemetry drops. Request latency sees only a failed capacity check and counter increment.
- Inspector reads stable active-table snapshots through sequence counters or asks the event loop through the bounded mailbox. It never locks request code.
- Multi-process aggregation is later: each process exposes a worker ID/socket and the CLI merges snapshots. Shared writable rings across processes are unnecessary initially.
- Free-threaded CPython, ordinary GIL, and optional JIT are separate test modes. Correctness relies on writer affinity, not the GIL.
- Shutdown: stop Inspector mutations, stop new captures, drain application work, publish final counters, give projector a bounded flush deadline, then discard remaining telemetry and report the count. Export never extends application shutdown.

## Correctness rules for the native spine

- Off must perform no context initialization, propagation, active-slot work, ring publication, or projector setup.
- Pulse emits no more than one completion cell and no phase cells.
- No full ring, full active table, failed projector, or full capture pool may block application work.
- Every drop has one bounded categorized counter; avoid recursive “telemetry about telemetry” events.
- Context and scratch ownership follows the request/stream lifetime exactly, including cancellation, reset, timeout, and connection loss.
- Router attribution must preserve route precedence, HEAD fallback, protected-ticket resolution, and current return shapes across trie/decision/bitset backends.
- Generic ASGI servers remain supported. Full native Pulse guarantees apply to Wreath's native server; a portable Python fallback must be labelled separately and benchmarked separately.
