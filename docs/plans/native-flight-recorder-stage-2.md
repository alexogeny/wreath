# Native Flight Recorder plan — Stage 2: control, export, recording, and replay

**Status:** partially implemented. This continues `native-flight-recorder-stage-1.md`
and uses Wreath's current name. The read-only Inspector (§5) has landed with
Stage 3; the OpenTelemetry, recording, and replay surfaces (§§6–8) remain the
plan of record. Progress is tracked box-by-box below and in the Stage-3 plan's
programme checklist.

## Implementation checklist (control/export/recording/replay)

This document specifies the Inspector, OpenTelemetry, recording, and replay
surfaces built by implementation stages 3–8 (see the Stage-3 plan's programme
checklist). The Inspector (§5) is built; the export/recording/replay surfaces
are not yet. The native spine, schema, IDs, context, ring, loss model, and
propagation they depend on are in place, and the Stage-4 projector core (the
off-path drain/assembly that §6 exports from) has landed.

- [x] §5 Inspector local protocol + public `wreath.inspector` + `wreath inspect`
      CLI (impl stage 3). *(read-only v1 landed 2026-07-19: HELLO/WORKERS/
      ACTIVE_REQUESTS/PRESSURE/EXPLAIN_ROUTE/EXPLAIN_PLAN/paged METADATA;
      JSON payloads inside the versioned binary frames until the stage-4
      projector emits TLV; TIMELINE/RECENT_FAILURES/CONNECTIONS/WEBSOCKETS/
      ROUTE_DISTRIBUTIONS and capture commands ride stages 4–5.)*
- [x] §6 OpenTelemetry projection + lazy Python bridge + optional OTLP adapter
      (impl stage 4, complete 2026-07-19). `wreath._projector` off-path
      drain/assembly (4a); `wreath._otlp` pure OTLP/JSON mapping — SERVER + phase
      child spans, request-count Sum + duration ExponentialHistogram, no OTel SDK
      dependency (4b); server-lifespan wiring so the projector is the ring's live
      consumer, Inspector TIMELINE/RECENT_FAILURES/ROUTE_DISTRIBUTIONS + CLI,
      `wreath._export` `ExportPipeline` + stdlib `OtlpHttpExporter`, and the lazy
      `wreath.telemetry.current_span`/`activate_otel` bridge (4c). The precise
      monotonic/wall calibration and the server-span-id read seam are the two
      deferred refinements, noted at their call sites.
- [x] §7 `WFR1` recording container + transport replay guarantee (impl stages 5–6).
      `wreath.recording` ships the `WFR1` container/sink/reader (stage 5); stage 6
      lands transport replay in `wreath.replay` — a `TransportRecording` (`WTR1`
      container), `replay_transport` driving the real native/pure HTTP/1 driver
      over a fake transport, `Date` normalization, and a checksummed `FaultSchedule`
      (`WFS1`) with the transport fault kinds (short-read/truncate/reset/half-close/
      clock-jump/duplicate/**timeout**) keyed to owned coordinates. The `timeout`
      fault fires the driver's *own* armed request/keep-alive deadline enforcement
      (native `_replay_fire_timeout` → the shared `enforce_deadline` C path, mirrored
      by the pure twin) — it drives the real owned mechanism, never a simulated
      outcome. HTTP/1 **and HTTP/2** replay (H2 via
      `replay_transport_h2` + the decode-only `wreath._h2_codec`, multiplex-aware);
      HTTP/3 transport replay is excluded by the QUIC per-connection-crypto
      boundary (documented in `reference/replay.md`), but H3 reaches parity via
      endpoint-plan replay and real-QUIC adversarial red-teaming.
- [x] §7 endpoint-plan replay + owned boundary adapters (impl stage 7).
      `replay_endpoint_plan` runs a `CanonicalRequest` through the owned routing/
      binding/validation/serialization with INVOKE (best-effort real handler),
      REPLACE (recorded return/exception through owned coercion + exception
      mapping), and SKIP (route resolution only) modes. Request-scoped boundary
      adapters (`ReplayAdapters`): `DatabaseDouble` (PostgreSQL — pool/query fault
      kinds, acquire/release accounting to prove no connection leak on the owned
      error path) and `FaultyHttpClient` (outbound — connect/read faults on the
      real client code path). `wreath replay transport|plan` CLI + `--inject`/
      `--record-faults`. REPLACE now re-runs the owned binding/validation with a
      signature-preserving substituted handler; adapter faults serialize through
      the checksummed schedule (`ReplayAdapters.from_faults`); ORM `Session`
      handlers replay through the same `DatabaseDouble` (installed on the registry).
      A curated fault corpus (`fault_corpus`) covers the whole §7 taxonomy and
      seeds the sanitizer gate.
- [x] §8 redaction/retention/security enforcement (impl stage 5) and deployment
      hardening (impl stage 8) — see `native-flight-recorder-stage-8.md` for the
      deployment posture, the enforcement/test map, and the shadow-execution gate.
- [x] §9 full configuration model: Detailed/Forensic modes, sampling/arming
      trigger table, exact fixed-memory validation (impl stages 3 + 5).

Landed already from this document's dependencies: the `TelemetryConfig`/`Mode`/
`RecordingPolicy` *value types* (validated, Stage 0) exist in `telemetry.py` /
`recording.py`; only Pulse-relevant fields are wired to the recorder so far.

## Goal

Expose the native spine through a bounded local Inspector, project its neutral records to OpenTelemetry off-path, add policy-controlled forensic recording, and define replay guarantees without claiming deterministic arbitrary Python execution.

## 5. Inspector protocol and public API

### Local protocol

Use a versioned, length-prefixed binary protocol over a Unix-domain socket on POSIX. A Windows named-pipe transport is a later portability item; never silently bind a public TCP port. Frames use a fixed header (`magic`, protocol version, command, flags, request ID, payload length) and bounded TLV payloads keyed by metadata IDs. Python/CLI formatting happens after receipt.

Security defaults:

- disabled unless configured;
- socket path created owner-only with safe no-follow handling;
- peer credentials checked where available;
- mutating commands require a startup-generated capability token in addition to local access;
- strict frame, response, list, and timeline limits;
- read-only and capture permissions separated;
- arm/disarm/export control actions recorded as bounded control events without secrets.

Required commands:

- `HELLO/CAPABILITIES`
- `WORKERS`
- `ACTIVE_REQUESTS`
- `CONNECTIONS`
- `WEBSOCKETS`
- `RECENT_FAILURES`
- `TIMELINE`
- `EXPLAIN_ROUTE`
- `EXPLAIN_PLAN`
- `ROUTE_DISTRIBUTIONS`
- `PRESSURE`
- `ARM_CAPTURE`, `DISARM_CAPTURE`, and `CAPTURE_STATUS`
- paged metadata lookup

Responses include snapshot generation IDs, truncation flags, and loss counters so clients can detect races or incomplete data.

### Provisional Python surfaces

Keep names literal and small:

- `wreath.telemetry.TelemetryConfig`, `Mode`, `SamplingPolicy`, `HistogramConfig`, `OTLPConfig`, `PropagationConfig`
- `wreath.recording.RecordingPolicy`, `CapturePolicy`, `RedactionPolicy`, `CaptureBudget`, `Trigger`
- `wreath.inspector.InspectorConfig`, `InspectorClient`, and immutable snapshot/result dataclasses
- `wreath.replay.open_recording`, `replay_transport`, `replay_endpoint_plan`, and replay result/difference types
- `Wreath(..., telemetry=...)` or explicit `app.configure_telemetry(...)`; reject changes after route compilation except atomic runtime arm/disarm operations designed for it
- a nested immutable operational config on `ServerConfig` for recorder/Inspector/export budgets

Do not expose raw ring pointers, dynamic request-time instrument creation, or a generic user event schema in v1.

### CLI

Extend the current argparse CLI in `src/wreath/_cli.py` with:

- `wreath inspect`
- `wreath capture arm|status|disarm`
- `wreath replay transport|plan`

The CLI is a protocol client and recording reader. It must not import the target application merely to inspect a worker. Support human tables and versioned JSON.

Inspector explanations join static metadata rather than duplicate runtime state: route path/method/operation ID, binding descriptor, dependencies, auth requirements/policies, middleware/finalizers, serializers/validators, limits, and phase-by-phase execution coverage (`native`, `python`, `mixed`, `external`, `unknown`).

## 6. OpenTelemetry projection and propagation

NFR remains the source model. OTLP/OpenTelemetry names, protobuf messages, batching, retries, resources, and exporter behavior exist only in projectors/adapters.

### W3C propagation

- Parse `traceparent` strictly in C when headers complete; reject all-zero IDs, invalid versions/flags/lengths, duplicates, and malformed hex without allocation.
- Generate trace/span IDs from a per-worker CSPRNG pool refilled off the request path. Pool exhaustion marks correlation loss; it never issues a request-path syscall.
- Treat `tracestate` and `baggage` as optional bounded/redacted propagation data. Do not copy them by default or use baggage as metric labels.
- Outbound Wreath HTTP requests receive IDs from explicit request context and format propagation into pre-sized stack/native buffers. The current Python client may do this in Python until a native protocol exists, only when configured.
- Map monotonic timestamps to Unix time in the projector using periodic monotonic/wall calibration pairs. Request code reads only a monotonic native clock.

### Projection

Completion records become server spans and request metrics. Detailed dependency events become child spans/events only when present. Route metadata supplies low-cardinality names and attributes. User/request/tenant IDs, raw paths, query values, SQL/database values, and header values are not metric labels.

The projector owns a bounded trace assembly table with expiry. Missing/dropped cells produce partial spans with explicit loss attributes; it never waits indefinitely for completeness. Export histograms from worker snapshots rather than reconstructing all metrics from completion cells.

OTLP support lives behind an optional adapter package/group or small exporter protocol. Neither the OpenTelemetry Python SDK nor `opentelemetry-cpp` is a core dependency. Export queue capacity, batch size, timeout, retries, and drops are bounded.

### Lazy Python bridge

`wreath.telemetry.current_span(request)` / `activate_otel(request)` lazily constructs or activates Python OTel context only when user code or third-party instrumentation asks. Entering a handler alone creates no SDK object. If OTel API packages are absent, return a Wreath-native immutable view or documented no-op bridge.

Bridge-created child spans correlate by trace/span IDs through a bounded control path. The native recorder must not depend on Python context variables.

## 7. Recording format and replay guarantees

### `WFR1` recording container

Use a chunked binary format:

- fixed header: magic, format/schema versions, endianness, features, application metadata hash, Wreath/Python/platform build IDs, clock calibration, recording UUID;
- metadata chunk: canonical ID tables and plan fingerprints;
- event chunks: native fixed records, checksum, sequence range, and loss counters;
- capture chunks: typed bounded fields referencing capture descriptors;
- external-effect chunks: request/response pairs for supported adapters;
- footer/index on clean close; readers recover complete checksummed chunks after abrupt termination.

Never write files from request code. The projector copies committed slabs and writes asynchronously. Disk-full/write failure drops recording output, updates counters, and never affects application work.

### Transport replay guarantee

Transport replay feeds recorded bytes/frames, segmentation, half-close/reset events, and virtual monotonic schedule into existing native HTTP/1, HTTP/2, HTTP/3, or WebSocket protocol drivers over fake transports.

It guarantees reproducible **Wreath-owned parser, framing, protocol-state, backpressure, and response-encoding behavior** for a compatible build/config/metadata image. Comparisons normalize explicitly variable fields such as Date and generated connection IDs.

It does not guarantee real kernel scheduling, TLS implementation behavior, uncaptured QUIC loss, peer network timing, or arbitrary ASGI application determinism.

### Endpoint-plan replay guarantee

Endpoint-plan replay starts from a canonical semantic request—method, route ID, path parameters, and policy-selected/redacted headers/query/body fields—then resolves a compatible plan descriptor and runs Wreath-owned routing, binding, validation, auth requirement evaluation, serialization, and boundary adapters.

Python handlers can be invoked, skipped, or replaced with a recorded return/exception according to replay mode. A run invoking arbitrary Python is labelled **best-effort execution**, never deterministic replay. Deterministic comparison ends at documented Wreath-owned boundaries unless every external effect and Python result is supplied by an adapter.

### Optional boundary adapters

Adapters are explicit and request-scoped:

- **Outbound Wreath HTTP:** method/origin/target, allowed headers/body hash or captured body, response/status/timing/errors.
- **Wreath PostgreSQL:** statement/query-plan ID, parameter shape and policy-approved values, result descriptor/rows or hash, transaction outcome and ambiguous completion.
- **Time:** virtual monotonic/wall reads only through an injected Wreath clock.
- **Randomness:** bytes only through an injected Wreath provider.
- **Future Wreath-owned services/jobs/webhooks:** use their owned effect IDs and explicit seams.

Do not monkeypatch arbitrary sockets, files, subprocesses, environment access, third-party clients, `time`, or `random`. Unadapted effects are reported and deterministic status is false.

Shadow execution is deferred. Preserve parent request IDs, boundary ordering, side-effect classification, and comparison fields so a future read-only shadow runner is possible. No live duplicate execution belongs in the initial implementation.

### Fault injection during replay

Replay is not only for reproducing a happy path. Its highest-value use is the
deterministic reproduction of *failure handling*: the parser rejecting a
truncated frame, a connection reset mid-body, a PostgreSQL statement erroring
after its pool wait, an export queue saturating. A fault-injection layer sits on
top of transport replay and endpoint-plan replay and perturbs a *compatible*
recording along Wreath-owned seams, so the owned recovery behavior — terminal
status, resource release, loss categorization, error mapping — can be exercised
under adversity and asserted to be itself deterministic.

**Determinism contract.** A fault schedule is an explicit, ordered list of
*fault descriptors*, each `(seam, trigger, kind, parameters)` where the trigger
is keyed only to stable owned coordinates — byte offset within a recorded
segment, frame sequence number, owned effect ID, virtual-clock instant, or the
Nth occurrence of an event kind — never wall-clock time or address. The schedule
is a first-class replay input: it round-trips through its own `WFR1`-adjacent
chunk, is checksummed, and makes an injected run bit-for-bit reproducible across
runs and builds. Fault injection never invents bytes the parser could not
otherwise receive; it only reorders, truncates, delays, drops, duplicates, or
substitutes at a seam the recording already models.

**Fault taxonomy by seam.**

- *Transport (transport replay).* Short/partial reads at arbitrary offsets;
  injected RST/FIN/half-close mid-message; HTTP/2 and HTTP/3 frame reordering,
  duplication, and interleave; flow-control window starvation and stalls;
  oversized, zero-length, and structurally malformed frames; HPACK/QPACK
  dynamic-table abuse; slow-peer pacing driven from the virtual schedule;
  modeled QUIC packet loss and reordering for HTTP/3; WebSocket fragment storms,
  unmasked/oversized frames, and mid-fragment close.
- *Scheduling (virtual clock).* Clock jumps and stalls; wakeups delivered a tick
  early or late; timeouts fired exactly at, just before, and just after their
  deadline (the boundary conditions real timing rarely hits reproducibly). A
  `timeout` fault is not a fabricated result: it fires the protocol driver's *own*
  armed request/keep-alive deadline enforcement — the same owned code the live
  timer callback runs (native `enforce_deadline`, reached through
  `_replay_fire_timeout`; the pure twin mirrors it) — so, e.g., an incomplete
  body-awaiting request emits a genuine `408` and an idle connection closes, each
  identically on both twins.
- *Boundary adapters (endpoint-plan replay).* PostgreSQL — pool-acquire timeout,
  server error after the round trip begins, lost commit acknowledgement
  (ambiguous completion), pool exhaustion, connection drop mid-result. HTTP
  client — DNS/connect/TLS failure, partial or truncated response, read timeout,
  retry-budget exhaustion. Time/randomness — provider exhaustion/starvation.
- *Sinks (projector/export/capture).* Ring undrained and full; phase-scratch and
  active-table exhaustion; export queue saturation; capture-slab exhaustion;
  disk-full and write failure; exporter slow, intermittently failing, and
  permanently failing.
- *Recording reader.* Truncated tail, corrupted chunk checksum, version and
  feature mismatch, reordered/duplicated chunks, and recordings whose loss
  counters are already non-zero (the reader must surface loss, not hide it).

**What it proves — and does not.** A fault-injection run proves that *Wreath-owned
handling of a modeled fault* is deterministic and safe: the same recording plus
the same fault schedule yields the same terminal status, the same categorized
loss counters, the same bytes on the wire up to normalized fields, and — under
sanitizers — no leak, use-after-free, or stale cross-extension pointer. It does
*not* claim to reproduce a real kernel, TLS stack, or QUIC path fault; it
reproduces the owned code path that such a fault would drive. Faults at Python
boundaries are only injected through adapters; a run that also executes arbitrary
Python stays labelled best-effort.

**Isolation.** Fault injection is a replay/test-only facility. It runs over fake
transports and injected adapters exclusively, cannot reach a real socket, file,
or subprocess, cannot broaden any capture or redaction policy, and has no
presence on a production request path. It shares the recording reader's
bounds-checking and the adapters' request-scoped seams.

**CLI and corpus.** `wreath replay transport --inject <schedule>` and
`wreath replay plan --inject <schedule>` apply a schedule; `--record-faults`
emits the realized schedule from a run so a discovered failure becomes a fixed
regression input. A curated fault corpus (one schedule per taxonomy entry, plus
fuzzer-discovered schedules) seeds the sanitizer and fuzz suites, so the
fault-injection library and the ASan/UBSan/fuzz gates are the same artifact.

**Acceptance.** Deterministic owned outcome across repeated runs and compatible
builds for every corpus entry; first-divergence diagnostics naming the seam and
trigger; the whole corpus clean under ASan/UBSan and the free-threaded build;
every modeled fault resolves to exactly one categorized loss/terminal outcome,
never a silent omission or an unbounded resource.

## 8. Redaction, retention, and security

Capture is deny-by-default and field-class aware.

- **Never capture by default:** Authorization, Proxy-Authorization, Cookie, Set-Cookie, API keys, TLS secrets, DSNs, database values, SQL parameters, outbound bodies, multipart/file content, or authentication artifacts.
- Headers use allowlist/drop/hash/constant-mask rules compiled to lower-case header IDs. Unknown headers drop.
- Request/response bodies default to length, media-type ID, and keyed hash. Content requires route + direction + media-type policy, maximum bytes, and truncation behavior.
- JSON/form capture requires structural field rules and bounded depth/field count. Invalid or streaming content falls back to drop/hash, never raw capture.
- Database capture keys by statement/plan and column/parameter ID. Values require explicit per-field policy. SQL text belongs in protected metadata, not event cells.
- Outbound capture keys by configured client/destination and controls requests/responses independently.
- Redaction happens before bytes enter a capture slab. Export-time-only redaction is insufficient.
- Use process-local keyed hashes for correlation without disclosure. Cross-process recording comparison receives a protected recording-key identifier, not the key.
- Enforce global, per-request, per-field, and per-route budgets plus expiry. Exhaustion truncates/drops and increments categorized counters.
- Inspector respects the same policy; local access does not bypass capture rules.
- Recording files are owner-only and may later support an application-provided encrypted sink. Core v1 should not invent cryptography.
- Runtime capture arms have authorization, expiry, and maximum matches, and cannot exceed the startup-compiled redaction ceiling.

Retention is explicit: recent-failure records, completed timelines, trace assembly entries, capture slabs, and files each have configured count/byte/time bounds. Expiry reclaims storage off-path. No “retain forever” mode exists in the in-process recorder.

## 9. Configuration model

Compile immutable policy in layers:

`server/worker defaults → application → included router metadata → route → endpoint plan`

A lower layer may narrow capture. Runtime control selects among precompiled policies but cannot exceed redaction or memory ceilings.

### Required modes

| Mode | Request-path behavior |
|---|---|
| **Off** | Null/zero recorder mode and one predicted static branch at native ingress/completion; no context initialization, propagation, active slot, counters, ring, or projector. |
| **Pulse** | Native correlation/propagation, active state, counters/histograms, compact timings, and optional single completion cell. No phase cells or payload copies. |
| **Detailed** | Pulse plus sampled/armed or promoted fixed phase cells and native-boundary/dependency timings. |
| **Forensic** | Detailed plus explicitly allowed bounded payload/external-effect capture into preallocated slabs. |

Separate `completion_summaries=False` from Pulse counters so aggregate metrics do not require one ring cell per request.

Configuration validation computes exact fixed memory from concurrency slots, route histogram policy, ring cells, capture slabs, metadata bytes, export queue, and trace assembly limit. Startup fails on arithmetic overflow, unsupported cardinality, or an unbounded option.

Dynamic arms compile off-path into an immutable trigger table and atomically swap a pointer/generation at an event-loop safe point. Supported predicates: error, latency, status, route/plan ID, trace ID, deterministic sample, and explicit token. Route patterns resolve to IDs at control/compile time, not as request-time string matches.

### Example shape (provisional, not a frozen API)

```python
TelemetryConfig(
    mode=Mode.PULSE,
    completion_summaries=True,
    ring_records=16_384,
    active_requests=2_048,
    histograms=HistogramConfig(per_route="selected"),
    detailed=SamplingPolicy(rate=0.001),
    recording=RecordingPolicy(
        capture_slabs=128,
        max_capture_bytes=8 * 1024 * 1024,
        redaction=RedactionPolicy.deny_by_default(),
    ),
)
```

Application/router/route overrides should refer to typed policy objects or registered policy names. Do not accept arbitrary request-time dictionaries.

## Inspector acceptance checks

- List active requests, workers, connections, WebSockets, and recent failures with bounded paging and snapshot generations.
- Inspect a request/trace timeline and clearly mark missing/dropped cells.
- Explain a compiled route/plan, including dependencies, auth policies, middleware, serializers, limits, and native/Python coverage.
- Display route-level latency and phase distributions from worker histograms.
- Arm/disarm captures without restart while enforcing startup redaction/memory ceilings.
- Report ring occupancy/high-water, categorized drops, exporter queue pressure, and capture slab usage.
- A malformed, unauthorized, slow, or disconnected Inspector client cannot block or allocate on request paths.

## OTel/recording/replay correctness rules

- OpenTelemetry is an export format, not NFR's internal schema.
- Exporter failure or backpressure only causes bounded queue/ring drops.
- Trace propagation rejects malformed input and never reflects unchecked values.
- Payload bytes are redacted before retention, not before export.
- Status/latency/error promotion cannot claim to recover payloads that were not pre-armed.
- Recording readers reject unsupported major versions and incompatible metadata instead of guessing.
- Replay reports the first incompatible/missing/unexpected boundary and whether the result remains deterministic.
- Arbitrary Python, third-party I/O, filesystem effects, and scheduler behavior are outside deterministic guarantees unless an explicit adapter owns them.
