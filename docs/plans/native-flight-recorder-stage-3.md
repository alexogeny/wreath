# Native Flight Recorder plan — Stage 3: implementation sequence and verification

**Status:** partially implemented. Implementation stage 0 and the functional core
of implementation stage 1 (native spine + HTTP/1 and HTTP/2 Pulse completion) have
landed; see `docs/decisions/0021-...` and `docs/decisions/0022-...` for the
decisions taken and the remaining Stage-1 follow-ups (bytes accounting, HTTP/3
hooks, acceptance benchmarks) and stages 2–8. The rest of this document remains
the plan of record. All names use Wreath rather than Neo.

## Programme checklist (implementation stages 0–8)

Tracks the whole NFR programme so progress is not lost between sessions. Each box
is the *functional core* of that stage; see the per-stage sections below for the
full evidence bar.

- [x] **Stage 0 — schemas, baselines, endpoint metadata.** `_flight_schema.py`
      (+ C mirror), pure container codec, deterministic metadata image + builder,
      validated config/policy value types.
- [x] **Stage 1 — native core, Off, Pulse completion.** Worker, ring, counters,
      histograms, active table, loss model; HTTP/1/2/3 hooks; bytes accounting;
      acceptance benchmark. (See the Stage-1 plan's checklist.)
- [x] **Stage 2 — propagation + route/plan attribution.** Strict W3C parse +
      correlation cell (done earlier); route/plan IDs stamped during dispatch on
      the native HTTP/1 fast path via the `_RequestContext` `flight` seam, joined
      to the Stage-0 metadata image, Off left branch-free. *(landed 2026-07-18)*
  - [x] Follow-up: extend route attribution to the HTTP/2 and HTTP/3 paths
        (they take the `__call__` path, which has no `_RequestContext`). The
        native protocol seeds `_wreath_flight` into the scope dict when a
        recorder is attached; dispatch overwrites it with `(route_id, plan_id)`;
        C reads it off the retained scope at completion before it emits the cell.
        *(landed 2026-07-18)* The pure ASGI (uvicorn) dict path never carries a
        recorder, so it is a no-op there beyond one dict membership test.
- [x] **Stage 3 — Detailed mode, triggers, read-only Inspector.** Fixed phase
      scratch, sampling/arming, promotion, dependency IDs, pressure snapshots,
      `wreath.inspector` + `wreath inspect` CLI. *(complete 2026-07-19; the
      Inspector commands that need projection/failure retention — TIMELINE,
      RECENT_FAILURES, CONNECTIONS, WEBSOCKETS, ROUTE_DISTRIBUTIONS — ride
      stage 4, and capture control rides stage 5, as scoped below.)*
  - [x] Slice 1 — Detailed-mode arming gate. `wreath_nfr_worker` carries a
        `detailed_sample_threshold`; `context_start` arms a deterministic
        per-request sample (stateless splitmix64 finalizer of the request id, so
        the decision is reproducible and does not perturb span/trace id
        generation) and sets `FLAG_DETAILED_ARMED`, which rides the completion
        cell. `Recorder(detailed_sample_rate=…)` and the pure oracle mirror it
        (byte-exact differential); the server threads `telemetry.detailed.rate`.
        Pulse never arms (Pulse cells unchanged). *(landed 2026-07-19)*
  - [x] Slice 2a — phase-scratch native core + wire format. Resolves ADR-0021's
        inline-vs-worker-slot question: a **separate worker-side scratch pool**
        (`phase_slots` blocks, sized to concurrent *armed* requests, not total
        concurrency), indexed by a per-context `phase_slot`. Off/Pulse and unarmed
        streams reserve nothing; the two context fields fit the struct's existing
        tail padding (zero inline growth). Records are 16 bytes, laid out in
        scratch as ring-ready 64-byte **phase-batch cells** (16-byte header + 3
        records, self-identifying by `request_id`), so commit is a straight cell
        copy — 3× less ring pressure than one cell per phase. `context_phase` C
        API (added to the capsule vtable), commit-behind-published-completion, budget +
        pool-exhaustion loss accounting, pure-oracle byte-exact twin,
        `phase_slots` config + memory budget. *(landed 2026-07-19)*
  - [x] Slice 2b (app path) — request-path phase markers for AUTH (both the
        classify-protected authenticate and the trie-mode `_authorize_request`),
        HANDLER, and SERIALIZE on the native HTTP/1 dispatch path. The context's
        `flight` member gained a third state (2 = recording *and* armed — gated
        on the ARMED flag, never `phase_slot` alone: an Off worker's early
        return leaves the context uninitialized where a zeroed `phase_slot` of 0
        is a valid slot), so dispatch's existing single member read now also
        answers "armed?" as a free int compare — request-trace verified zero
        added crossings for Off/Pulse/unarmed. `_flight_phase(phase_id, dep_id,
        coverage, duration_ns)` takes only a duration; C anchors the start
        offset in its own `PyTime_MonotonicRaw` clock against `ctx->start_ns`
        so offsets never mix clock bases with Python's `monotonic_ns`.
        *(landed 2026-07-19)*
  - [x] Slice 2b (dependency seams) — PostgreSQL and HTTP-client markers with
        dependency IDs, landed on top of the escape-safe C contract: the
        protocol retains an *armed* request's context (`nfr_http_scope`, like
        the WS path's `nfr_ws_scope`) and severs its borrowed recorder
        pointers right after `context_end` (`wreath_request_context_sever`),
        so a `_flight_phase` binding that escaped into a spawned task or
        background hook is an inert no-op, never a stale write. Propagation is
        `wreath._flight_markers.phase_marker` (a ContextVar dispatch sets only
        when armed; unarmed dependency calls pay one `ContextVar.get`).
        Seams: `Database.acquire` → DB_POOL_WAIT, `Statement._call`/`.map` →
        DB_QUERY, `HTTPClient.request` → HTTP_CLIENT (get/post delegate).
        Dependency IDs come from the metadata image: HTTP clients are now
        interned into the image's `clients` table, and the lazy
        `_build_flight_route_ids` join stamps `_flight_dep_id` onto each live
        Database/HTTPClient. ORM reads that ride Statement/acquire are covered;
        a dedicated ORM_HYDRATE marker waits for the ORM-session seam.
        *(landed 2026-07-19)*
  - [x] Slice 3a — completion promotion. A Detailed completion that is slow
        (duration ≥ `detailed_slow_us`) is flagged `SLOW_PROMOTED`; an
        errored/timed-out one is flagged `ERROR_PROMOTED`. The flags ride the
        completion cell that is emitted anyway (no ring cost, no crossing) and
        cannot recover un-armed phases (ADR risk #4, stated in the config docs).
        Pulse never promotes — its cells stay byte-identical to Stage 2. Worker
        `slow_threshold_us`, `Recorder(detailed_slow_us=…)`, `TelemetryConfig.
        detailed_slow_us`, pure-oracle twin, server wiring. *(landed 2026-07-19)*
  - [x] Slice 3b — pressure snapshots: `phase_capacity` / `phase_in_use` /
        `phase_high_water` Recorder gauges beside the ring/active snapshots
        (relaxed atomics like `ring_high_water`, updated in reserve/release;
        the pure oracle mirrors all three). *(landed 2026-07-19)*
  - [x] Slice 4 — read-only Inspector protocol + `wreath inspect` CLI.
        `wreath.inspector`: versioned 16-byte-header frames (`WFI1`) over an
        owner-only (0600) Unix socket, disabled unless configured, SO_PEERCRED
        same-UID check, strict frame/paging limits, idle-timeout disconnect,
        one error frame then close on malformed input. Read-only commands:
        HELLO/CAPABILITIES, WORKERS, ACTIVE_REQUESTS (seqlock active-table
        snapshot, new `Recorder.active_snapshot()` + pure twin), PRESSURE
        (ring/phase/active gauges + loss counters), EXPLAIN_ROUTE,
        EXPLAIN_PLAN, paged METADATA — all with generation IDs and truncation
        flags. Server wiring: `ServerConfig.inspector` (honored only when
        telemetry created a recorder); closed first on shutdown. CLI is a pure
        protocol client (never imports the app), human tables or versioned
        JSON. v1 payloads are JSON inside the binary frames; the TLV binary
        projection lands with the stage-4 native projector. TIMELINE,
        RECENT_FAILURES, CONNECTIONS, WEBSOCKETS, ROUTE_DISTRIBUTIONS, and
        capture commands need stage-4/5 machinery (projection, failure
        retention, capture) and ride those stages. *(landed 2026-07-19)*
- [x] **Stage 4 — asynchronous projection + OTel.** Native drain/projector
      thread, bounded trace assembly/export queue, OTLP mapping behind an optional
      adapter, lazy Python bridge. *(complete 2026-07-19)*
  - [x] Slice 4a — projector core. `wreath._projector.Projector` owns one
        background thread that drains the ring in bounded batches through the
        recorder's public `drain`, reassembles each request off-path (a
        completion joined to its correlation carrier and detail phases), and
        keeps bounded windows of recent completions and failures plus per-route
        metric aggregates for the Inspector and exporters. Assembly settles a
        completion after a *quiet cycle* — one drain cycle with no further cell
        for that request — which is robust to the fixed completion→correlation→
        phase publish order and to a batch boundary that splits the tail;
        headless cells (dropped ring head) are counted as orphans, never
        emitted. An optional `on_trace` export hook runs on the projector
        thread with its failures isolated to a counter, so no exporter code
        ever touches a request stack. Pure Python over the recorder's public
        accessors, so it drives the native `Recorder` and the pure oracle
        identically. Tests: `tests/test_flight_projector.py` (22, incl. two
        end-to-end over a real native Detailed/Pulse recorder). *(landed
        2026-07-19)*
  - [x] Slice 4b — OTLP mapping (pure). `wreath._otlp` maps a `ProjectedTrace`
        to an OTLP/JSON `ExportTraceServiceRequest` (one SERVER span per
        completion; detail phases become CLIENT/INTERNAL child spans with
        deterministic derived span ids) and a `ProjectorSnapshot` to an
        `ExportMetricsServiceRequest` (a cumulative request-count Sum and a
        request-duration ExponentialHistogram — the recorder's base-2 log
        buckets are exactly scale-0 exponential buckets). Span wall-clock is
        anchored on the projector's observation time (`start = observed -
        duration`); names/attributes come only from route metadata (method +
        template), never concrete paths/queries/headers/SQL, so cardinality is
        bounded by construction. No OpenTelemetry SDK is imported. Defines the
        `SpanExporter`/`MetricExporter` protocols and a `BoundedExportQueue`
        (fixed capacity, drop-counted) for the export path. Tests:
        `tests/test_flight_otlp.py` (16, incl. an end-to-end real-recorder →
        projector → OTLP → `json.dumps`). *(landed 2026-07-19)*
  - [x] Slice 4c — projection made live end-to-end. The server now creates and
        starts the projector whenever a recorder exists (it is the ring's only
        consumer: before this, a running server's completions accumulated and
        dropped), joins its thread off the loop on shutdown after a final drain,
        and passes it to the Inspector. New Inspector commands read a projector
        snapshot — TIMELINE (recent completions, newest first), RECENT_FAILURES
        (non-OK/5xx/promoted), ROUTE_DISTRIBUTIONS (per-route count/errors/
        duration + buckets) — advertised in HELLO capabilities only when a
        projector is attached, with `wreath inspect {timeline,failures,
        distributions}` CLI topics. OTLP export rides `wreath._export`: an
        `ExportPipeline` (bounded queue fed by the projector's `on_trace`, a
        dedicated exporter thread that batches + maps + pushes, every transport
        call isolated to a counter) plus `OtlpHttpExporter` (OTLP/HTTP+JSON over
        stdlib `urllib` — enabling export pulls in no third-party dependency),
        wired when `telemetry.otlp.enabled`. Lazy OTel bridge in
        `wreath.telemetry`: `current_span(request)` returns an immutable
        `SpanContextView` parsed from the incoming `traceparent` (no SDK object);
        `activate_otel(request)` hands that context to the OTel API only if it is
        importable and only at the call site, else returns the native view.
        Tests: `test_flight_export.py` (12), `test_flight_inspector_projection.py`
        (6, incl. CLI), `test_flight_server_lifecycle.py` (3, real loopback
        server drains its ring), `test_flight_bridge.py` (7). *(landed
        2026-07-19)*
        Deferred refinements: precise monotonic/wall calibration (needs a
        per-cell monotonic stamp — a schema change) and exposing the server's own
        generated span id to the bridge (needs a native read seam); both noted at
        their call sites.
- [x] **Stage 5 — forensic capture, redaction, Inspector control.** Preallocated
      capture slabs, deny-by-default redaction ceiling, arm/disarm, `WFR1`
      writer/reader, `wreath capture` CLI. *(functional core complete 2026-07-19;
      slices 5a–5e below. Follow-ons: body/response/DB/outbound capture, per-arm
      narrowing, H2/H3 capture paths.)*
  - [x] Slice 5a — native capture-slab core. A preallocated slab pool in the
        worker (writer-owned free stack + SPSC commit/return index rings),
        lazily reserved per Forensic-armed request, with disposition enforced as
        bytes are written: `RAW` copies a bounded (truncating) prefix, `HASHED`
        stores an 8-byte SipHash-2-4 keyed digest and never the bytes,
        `MASKED`/`LENGTH` retain only the original length — so a disallowed
        field's raw bytes never live in recorder memory. Commit rides behind the
        published completion like phases; slabs are self-identifying and copied
        out off-path by `Recorder.drain_captures`. The capsule vtable exposes
        `context_capture`. Byte-exact pure oracle (`CaptureSlab`, pure SipHash)
        and `tests/test_flight_capture.py` (deny-by-default, differential
        parity, exhaustion, truncation, secret canaries); clean under
        ASan/UBSan incl. a threaded sink drain (`tools/sanitizers/build_flight.py`).
        *(landed 2026-07-19)*
  - [x] Slice 5b — redaction policy compilation. `wreath.recording` gained the
        hash/mask header dispositions beside the Stage-0 allowlist, a layered
        `narrow`, and `compile_redaction` → an immutable `CompiledRedaction` with
        deterministic 1-based header descriptor ids and per-direction body
        dispositions the request-path seam consults. Tests:
        `tests/test_flight_recording.py` (incl. composition with the native
        core: deny-by-default drops, secrets never plaintext). *(2026-07-19)*
  - [x] Slice 5c — `WFR1` writer/reader + async recording sink.
        `wreath._recording_format`: a streaming `WFR1Writer` (header with image
        hash / UUID / clock calibration / build id, `META` + `CAPT`/`EVNT` chunks,
        `FOOT` on clean close), `read_recording` (rejects bad version/hash,
        recovers a torn/corrupt tail, reports `clean`), and `RecordingSink` — the
        sole capture-slab consumer on its own thread, owner-only file, disk-error
        → drain-and-drop + count, never touching request work. Tests:
        `tests/test_flight_recording_format.py`. *(2026-07-19)*
  - [x] Slice 5d — Inspector capture control + `wreath capture` CLI.
        `ARM_CAPTURE`/`DISARM_CAPTURE`/`CAPTURE_STATUS` behind a capability token
        (`hmac.compare_digest`, separate from read access), advertised only with
        an `ArmRegistry` + token; the registry enforces the startup ceiling,
        positive expiry, max-matches, and a concurrent-arm cap. Tests:
        `tests/test_flight_capture_control.py`. *(2026-07-19)*
  - [x] Slice 5e — capture made live. Server creates a Forensic recorder *with*
        its capture pool, compiles the `RecordingPolicy`, starts the
        `RecordingSink` + `ArmRegistry` beside the projector, and installs the
        plan on the app; the native `_RequestContext._flight_capture` seam (gated
        forensic-armed, mirrors `_flight_phase`) captures request headers per the
        plan when an arm is active. End-to-end over loopback → `WFR1` file;
        `wreath-request-trace --check` adds **no crossings** for Off/Pulse/
        Detailed; server seam ASan/UBSan-clean. *(2026-07-19)* Follow-ons: body /
        response / DB / outbound capture, per-arm narrowing, H2/H3 dict-scope.
  - Stage 5 functional core is **complete**: Forensic capture is live end-to-end
    (native slab pool → policy → request seam → WFR1 sink → Inspector control).
- [ ] **Stage 6 — transport replay.** Recording-backed fake transports + virtual
      scheduling for HTTP/1/2/3 + WebSocket, `wreath replay transport`, and the
      transport-seam half of fault injection (`--inject`/`--record-faults`, the
      deterministic fault-schedule chunk, and the transport/scheduling/sink fault
      taxonomy — see stage-2 §7 "Fault injection during replay"). *(not started)*
- [ ] **Stage 7 — endpoint-plan replay + owned adapters.** Canonical semantic
      inputs, plan compatibility, replay modes, HTTP/PostgreSQL/time/random
      adapters, `wreath replay plan`, and the adapter-seam half of fault
      injection (pool timeout, ambiguous commit, client failures, provider
      exhaustion). *(not started)*
- [ ] **Stage 8 — deployment hardening + shadow design gate.** Multi-process
      Inspector aggregation, soak/fault campaigns, compatibility policy, shadow
      ADR. *(not started)*

## Goal

Deliver the shared native spine in independently testable slices, proving disabled and Pulse costs before adding Inspector, OTLP, payload capture, or replay. Each slice below states exact scope, affected surfaces, dependencies, required evidence, completion criteria, and deliberate deferrals.

## 10. Incremental implementation stages

### Implementation stage 0 — schemas, baselines, and endpoint metadata

**Exact scope**

Specify the C ABI, fixed event schema, metadata image, recording primitives, deterministic ID assignment, mode/config dataclasses, exact memory accounting, and immutable endpoint-plan descriptors. Record no runtime telemetry.

**Native and Python surfaces**

- New private schema header and pure reference codec.
- `router.RouteDefinition`, `binding.BindingSpec`, `compile_binder()` descriptor extraction, and `Wreath._compile_routes()`.
- Reserved modules may expose experimental config/value types only; no functional telemetry API.

**Dependencies on earlier stages:** none.

**Tests and benchmarks**

- Canonical IDs/images across processes and registration order where order is not semantic.
- Metadata hash changes for semantic changes and remains stable for process-local differences.
- Config arithmetic overflow, cardinality, invalid mode, and bounded-size rejection.
- Pure codec round-trip, corruption, truncation, and size limits.
- Repeated telemetry-free baselines for empty/static/JSON/stream/WebSocket/database/client workloads.
- Existing `wreath-request-trace` baseline.

**Completion criteria**

- The same application produces byte-identical metadata.
- Endpoint explanations cover current dependencies, policies, middleware, serializers/validators, and limits.
- No request code, Python/native crossing count, or benchmark behavior changes.

**Deliberately deferred:** recorder memory, rings, Inspector, OTel, payloads, replay execution.

### Implementation stage 1 — native core, Off, and Pulse completion

**Exact scope**

Add `_flight`, worker ownership, active slots, counters/histograms, SPSC ring, loss accounting, context/scratch layouts, and HTTP/1/2/3 ingress/completion hooks. Off remains default. Pulse commits zero or one completion cell according to config.

**Native and Python surfaces**

- `setup.py`; new `_native/flight.h`, `flight.c`, `_flightmodule.c`.
- Server request/connection/stream structs and all completion/error/reset paths.
- `Server` recorder create/attach/shutdown lifecycle.
- Bounded pure oracle for record/ring behavior.

**Dependencies:** implementation stage 0 schemas.

**Tests and benchmarks**

- C ring wrap, sequence, full/drop, and memory-order tests.
- Active-slot generation/reuse, capacity, snapshot, cancellation, and teardown.
- Fake-transport exact completion cells for success, error, cancellation, disconnect, timeout, streaming, and every built protocol.
- Free-threaded mode plus ASan/UBSan.
- Undrained/full ring and absent/dead reader.
- Interleaved A/A Off versus a telemetry-free build.
- Pulse CPU, throughput, p50/p99/p999, allocations, instructions/cache misses, and RSS on empty/high-concurrency workloads.

**Completion criteria**

- Off is statistically indistinguishable and crossing-identical.
- Pulse meets the under-1% target or evidence explicitly records that it does not.
- An undrained/full ring has no measurable request-latency effect beyond noise.
- Startup memory calculation agrees with observed allocations/RSS within documented allocator overhead.

**Deliberately deferred:** route attribution, detailed phases, capture, Inspector, exporters.

### Implementation stage 2 — propagation, route/plan attribution, metadata joins

**Exact scope**

Add strict W3C parsing/generation, route and plan IDs written during existing native router calls, route histograms, and native/Python coverage metadata. Keep public match result shapes unchanged.

**Native and Python surfaces**

- `_flight` propagation C API.
- Native and pure trie/decision/bitset route records.
- `_routing.py`, `app._compile_routes`, request-context access.
- Explicit outbound-client propagation seam.

**Dependencies:** implementation stages 0–1.

**Tests and benchmarks**

- W3C valid/invalid corpus, duplicates, all-zero IDs, versions/flags, fuzzing.
- Router differential/parity across backends; precedence, HEAD, miss, static, protected ticket, resolve, and path params.
- ID exhaustion and metadata mismatch.
- Inbound/outbound propagation integration.
- `wreath-request-trace` proves no extra Pulse crossing.
- Static/parameter/protected/miss and small-JSON benchmarks.

**Completion criteria**

- Every completion is correctly attributed or explicitly `unknown`.
- Malformed propagation is rejected safely without reflected unchecked bytes.
- Route counters/distributions match independently counted requests.

**Deliberately deferred:** Python OTel objects, phase markers, payload capture.

### Implementation stage 3 — Detailed mode, triggers, and read-only Inspector

**Exact scope**

Implement fixed phase scratch, sampling/arming, completion promotion for slow/error/status, dependency IDs, pressure snapshots, and read-only local Inspector protocol/CLI.

**Native and Python surfaces**

- Armed-only markers in `app.py`, `http_client.py`, `postgres.py`, ORM session/plan seams, serializers, and response completion.
- Worker mailbox/snapshot API.
- `wreath.inspector` and `wreath inspect` CLI.

**Dependencies:** implementation stages 1–2.

**Tests and benchmarks**

- Exact lifecycle ordering and all early exits.
- Scratch/ring overflow, trigger precedence, expiry, deterministic sampling.
- Snapshot races/generation reuse.
- Malformed, oversized, unauthorized, slow, and disconnected Inspector clients.
- Active request/connection/WebSocket/failure lists and route/plan explanations.
- Detailed rates 0%, 0.1%, 1%, and 100% across streaming, WebSockets, database, and outbound HTTP.

**Completion criteria**

- Required read-only Inspector capabilities work with paging/loss markers.
- Pulse remains crossing-identical to stage 2.
- Detailed cost/output scale with armed rate.
- Reader/control activity cannot block the writer.

**Deliberately deferred:** mutating capture commands, OTLP, payloads, replay.

### Implementation stage 4 — asynchronous projection and OTel

**Exact scope**

Add native drain/projector boundary, bounded trace assembly/export queue, worker metric snapshots, OTLP mapping, exporter failure isolation, monotonic/wall calibration, and lazy Python OTel bridge.

**Native and Python surfaces**

- `wreath.telemetry`.
- Private projector service and optional OTLP adapter/dependency group.
- Server lifespan integration.
- Lazy request bridge methods.

**Dependencies:** implementation stages 1–3.

**Tests and benchmarks**

- Golden projected traces/metrics and route attributes.
- Timestamp calibration/drift and partial/lost traces.
- OTel packages absent; bridge requested/not requested.
- Export timeout, retry, backpressure, permanent failure, queue saturation, and shutdown deadline.
- Normal export and failed-exporter request benchmarks.
- Exported-record/counter accuracy against independent counts.

**Completion criteria**

- No SDK/exporter code appears on request stacks.
- Exporter failure has no measurable p99 effect.
- Spans/metrics match source records and explicitly represent loss.
- Lazy bridge creates no Python OTel object unless requested.

**Deliberately deferred:** payload recording and replay.

### Implementation stage 5 — Forensic capture, redaction, and Inspector control

**Exact scope**

Implement preallocated capture slabs, startup redaction ceiling, policy-controlled header/body/database/outbound fields, arm/disarm commands, expiry/match limits, and `WFR1` writer/reader.

**Native and Python surfaces**

- Native capture hooks at server/client/PostgreSQL owned boundaries.
- `wreath.recording`.
- Inspector mutation authentication and `wreath capture` CLI.
- Asynchronous recording sink.

**Dependencies:** implementation stages 0–4.

**Tests and benchmarks**

- Secret-canary tests for credentials, headers, cookies, bodies, SQL parameters, database rows, DSNs, and outbound traffic.
- Structured-redaction depth/field/invalid-input limits.
- Per-field/request/route/global truncation and slab exhaustion.
- Arm authorization, expiry, match count, and startup-ceiling enforcement.
- Disk-full, sink failure, crash recovery, checksums, version rejection.
- Large JSON/file/streaming/WebSocket/outbound/database capture.
- Forensic rates 0%, 0.1%, 1%, and budget saturation.

**Completion criteria**

- Forbidden bytes never enter slabs or files.
- Memory remains inside the configured budget.
- All truncation/drop/pressure is visible.
- Runtime arms cannot broaden startup policy.

**Deliberately deferred:** encryption implementation and deterministic replay.

### Implementation stage 6 — transport replay

**Exact scope**

Add recording-backed fake transports and virtual scheduling for HTTP/1, HTTP/2, HTTP/3, and WebSocket protocol state, plus normalized output comparison and first-difference diagnostics.

**Native and Python surfaces**

- Existing deterministic protocol feed hooks/fixtures.
- `wreath.replay` transport API and `wreath replay transport` CLI.
- Recording reader and compatibility checks.

**Dependencies:** implementation stage 5 format and stage 0 metadata.

**Tests and benchmarks**

- Golden captures for fragmentation, pipelining, flow control, reset, timeout, malformed input, backpressure, disconnect, and streaming.
- Same-capture repeatability.
- Incompatible image/version/build rejection.
- Sanitizer/fuzzer seed integration.
- Replay throughput and bounded-memory measurement (not a production-path claim).

**Completion criteria**

- Wreath-owned parser/framing/protocol/encoding behavior repeats byte- or semantic-equivalently.
- Normalized fields are explicit.
- Unsupported kernel/TLS/QUIC/application effects are reported, not hidden.

**Deliberately deferred:** Python-handler determinism, endpoint effects, live shadowing.

### Implementation stage 7 — endpoint-plan replay and owned adapters

**Exact scope**

Add canonical semantic inputs, plan compatibility checks, replay modes (skip, recorded result, invoke Python), HTTP/PostgreSQL/time/random adapters, effect ordering, and deterministic-status reporting.

**Native and Python surfaces**

- `wreath.replay` endpoint-plan API and CLI.
- Endpoint descriptors.
- Explicit request-scoped adapter injection in Wreath-owned HTTP, PostgreSQL, clock, and randomness seams.
- Testing utilities.

**Dependencies:** implementation stages 0, 5, and 6.

**Tests and benchmarks**

- Binding/validation/auth-requirement/serialization parity.
- Database result/transaction success, failure, and ambiguous completion.
- Outbound retry and error sequences.
- Virtual time/random behavior.
- Missing, unexpected, duplicated, and reordered effects.
- Python nondeterminism labelled best-effort.
- Verify absent adapters do not change Pulse production paths.

**Completion criteria**

- Deterministic status is true only when every required boundary is virtualized or recorded.
- Arbitrary Python is never advertised as deterministic.
- Diagnostics identify the first incompatible plan/effect/result.

**Deliberately deferred:** filesystem/subprocess/third-party effects, distributed capture, shadow execution.

### Implementation stage 8 — deployment hardening and shadow design gate

**Exact scope**

Add multi-process Inspector aggregation, Windows local transport if supported, soak/fault campaigns, compatibility policy, and a measured design review for read-only shadow execution. Shadow implementation requires a separate accepted ADR.

**Native and Python surfaces:** deployment/control tooling and docs; no assumed request-path redesign.

**Dependencies:** all earlier implementation stages.

**Tests and benchmarks**

- Multi-worker ordering/loss and process crash/restart.
- Long exporter/capture pressure soak.
- GIL/free-threaded/JIT matrices.
- Format upgrade/downgrade corpus.
- Security review and control-socket abuse cases.

**Completion criteria**

Operational limits and compatibility are supported by retained evidence. Any shadow proposal proves write suppression, isolation, bounded resource use, and opt-in behavior.

**Deliberately deferred:** arbitrary deterministic Python, cross-host shared-memory rings, unrestricted shadow writes.

## 11. Files and modules expected to change

### New files

```text
src/wreath/_native/flight.h
src/wreath/_native/flight.c
src/wreath/_native/_flightmodule.c
src/wreath/_pure/flight.py
src/wreath/_flight_schema.py
src/wreath/_projector.py
src/wreath/_inspector_protocol.py
src/wreath/_recording_format.py

tests/test_flight_schema.py
tests/test_flight_native.py
tests/test_flight_propagation.py
tests/test_inspector.py
tests/test_recording.py
tests/test_replay_transport.py
tests/test_replay_plan.py

benchmarks/bench_flight_recorder.py
```

Split `flight.c` into ring/capture/propagation units only when size or profiling justifies it.

### Existing files likely changed

```text
setup.py
pyproject.toml
src/wreath/_native/server.h
src/wreath/_native/server_request.c
src/wreath/_native/server_http1.c
src/wreath/_native/server_http2.c
src/wreath/_native/http3.h
src/wreath/_native/http3_asgi.c
src/wreath/_native/dtrouter.c
src/wreath/_native/dtbitset.c
src/wreath/_native/router.c
src/wreath/_routing.py
src/wreath/app.py
src/wreath/router.py
src/wreath/binding.py
src/wreath/request.py
src/wreath/server.py
src/wreath/http_client.py
src/wreath/postgres.py
src/wreath/telemetry.py
src/wreath/inspector.py
src/wreath/recording.py
src/wreath/replay.py
src/wreath/_cli.py
repo-map.md
docs/agents/manifest.json
docs/reference/roadmap.md
mkdocs.yml
docs/llms.txt
```

Add focused changes to ORM session/registry/compiler, serializers/responses, WebSocket, protocol, PostgreSQL, and sanitizer suites only at the stage that instruments them.

When a reserved public module becomes functional, follow `docs/cookbook/agents/documenting-a-module.md`: add reference, guide, recipes where useful, nav, and `llms.txt`, and remove its roadmap row.

Do not combine NFR with native-client completion, ORM redesign, multi-process server supervision, migrations, or broad rename cleanup.

## 12. Benchmark and correctness matrix

### Workloads and modes

Run Off, Pulse without summaries, Pulse with summaries, Detailed at several sample rates, and Forensic at several capture rates against:

- empty native response and empty Python handler;
- small/large JSON with validation and serialization;
- request/response streaming, large bodies, slow peers, and backpressure;
- WebSocket connect/message/fragment/ping/close and many idle sockets;
- HTTP/1 keep-alive/pipelining, HTTP/2 concurrency/flow control, HTTP/3 when built;
- concurrency to active-table capacity and beyond;
- PostgreSQL pool wait + read/write/transaction and ORM hydration;
- outbound HTTP DNS/connect/TLS/reuse/retry/large response;
- exporter healthy/slow/failing, ring undrained/full, capture pool full, sink full;
- route misses, protected allow/deny, exceptions, cancellation, timeout, disconnect, malformed protocol input.

### Required measurements

Retain repeated raw trials and environment metadata: Python/build mode, compiler/flags, platform/CPU governor, event loop, protocol/server, concurrency, duration, warmup, recorder budgets, sampling/capture rates, and exporter state.

Measure cycles, instructions, branches/cache misses, allocations, RSS/peak RSS, CPU, throughput, p50/p99/p999, ring/capture/export drops, occupancy high-water, and exported/recorded accuracy. Establish A/A noise before attribution.

Ablate at whole-request level: context only, active table, counters, histogram, completion ring, propagation, and projector independently. Below-noise results are unresolved, not wins.

### Acceptance targets

- **Off:** statistically indistinguishable from telemetry-free and identical `wreath-request-trace` crossings.
- **Pulse:** target below 1% CPU or throughput regression.
- **Latency:** no measurable p99 increase during normal export; always report p999.
- **Failure isolation:** exporter/projector/disk backpressure causes drops only and no measurable request-latency effect.
- **Memory:** high-water remains inside computed fixed budgets plus documented projector/allocator/OS overhead.
- **Scaling:** Detailed/Forensic cost and volume track armed/capture rates.
- **Accuracy:** independent request/status/route/phase counts agree; discrepancy is represented by categorized loss, never silent omission.

### Correctness/security suites

Include schema parity, protocol fake transports, routing differential tests, W3C corpus/fuzzing, ring model/wrap/generation tests, free-threaded atomics, ASan/UBSan, malformed Inspector/recording fuzzing, crash recovery, secret canaries, cancellation/shutdown, format compatibility, replay first-difference diagnostics, and effect-adapter ordering.

Run focused tests at each stage, then the project gates appropriate to changed files: `uv run pytest`, Ruff, ty, native lints, request trace, sanitizers for touched C, strict docs when public docs change, and full marked tests where network/fuzz/performance behavior changes.

## 13. Risks and unresolved decisions

### Principal risks

1. **Current lifecycle boundary:** native server transport is C-owned, but application orchestration/handlers are Python. Pulse must piggyback on existing native calls; Detailed Python phases are opt-in.
2. **Cross-extension lifetime/ABI:** capsule version handshakes and teardown order must prevent stale pointers.
3. **Cache footprint:** inline context/scratch may hurt disabled/common HTTP/2/3 streams. Measure inline storage against preallocated worker slots referenced by index.
4. **Retroactive triggers:** slow/error promotion cannot recover payloads not pre-armed. APIs must say this plainly.
5. **Cardinality:** per-route histograms can dominate large route sets. Support global-only, selected-route, or capped policies.
6. **Clock/trace correctness:** calibration, entropy pools, fork behavior, and malformed propagation need dedicated review.
7. **Inspector attack surface:** local control can expose operational data or arm expensive capture. Permission separation, bounds, audit, and expiry are mandatory.
8. **Recording secrecy:** malformed/streaming structured bodies make redaction hard. Fall back to hash/drop; never store raw bytes for later redaction.
9. **OTel semantic drift:** version projection mappings without shaping internal cells around OTLP.
10. **Replay overclaim:** Python, third-party I/O, files, external systems, and scheduling remain nondeterministic unless adapted.

### Decisions for implementation stage 0

- Exact context, scratch, and cell sizes.
- Inline scratch versus worker-slot storage.
- One mixed ring versus separate completion/detail/control rings.
- Histogram bucket scheme and selected-route memory policy.
- Projector thread versus helper process and minimal optional exporter contract.
- Metadata encoding/hash and compatibility rules.
- Inspector TLV details and Windows transport.
- Trace entropy refill and fork handling.
- Whether HTTP/2/3 gain lazy `_wreath_http` context as a separate measured change.
- Capture encryption sink interface (not an in-core cipher).
- Endpoint deterministic boundary when auth backends/policies are Python.

## Explicit non-goals

- Deterministic replay of arbitrary Python or third-party code.
- Replacing ASGI, asyncio/uvloop, parsers, routing, middleware, clients, PostgreSQL, or ORM execution to simplify telemetry.
- Mandatory OpenTelemetry dependencies or OTLP as the internal schema.
- Per-request formatted logging, dynamic instruments, unbounded labels, or a generic logging framework.
- Request-path exporter work, I/O, protobuf, formatting, allocation, locks, or Python callbacks.
- Packet capture, TLS key logging, or unrestricted credential/body/database capture.
- A new multi-process supervisor, tracing backend, profiler, debugger, or general fault-injection system.
- Live shadow execution before recording/replay guarantees and write isolation have a separate accepted design.

## Recommended first implementation slice

Implement only implementation stages 0 and 1 first: deterministic metadata/config, separately importable `_flight`, Off/Pulse worker state, and one compact completion cell across existing protocol tests. Do not start with OTLP or an Inspector UI. This proves the hardest invariants—disabled cost and non-blocking bounded publication—while leaving later features on the same IDs, context, ring, and loss model.
