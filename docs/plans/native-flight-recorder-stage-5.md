# Native Flight Recorder plan — Stage 5: forensic capture, redaction, Inspector control

**Status:** landed. Stages 0–4 preceded it; Stage 5 adds the *only* part of the
recorder that ever copies application bytes — so its whole design is built around
one invariant: **forbidden bytes never enter a slab or a file**, and a runtime
arm can never broaden what startup already refused. Capture covers request/
response headers, query parameters, request/response bodies, DB parameters and
rows, and outbound bodies, across HTTP/1, HTTP/2, and HTTP/3 (a Wreath app
dispatches all three through the same owned request context). All names use Wreath.

This document is the plan of record for Stage 5. It slices the stage the way
Stages 1–3 were sliced: the hardest native invariants first (a bounded,
deny-by-default slab pool that redacts *before* a byte is retained), then the
policy compiler that decides what may be captured, then the off-path `WFR1`
writer/reader and sink, then the Inspector arm/disarm control surface and CLI.

## Where Stage 5 sits

Stage 4 left the recorder with a live off-path projector draining the ring and
an optional OTLP export pipeline. Stage 5 adds a **second** off-path consumer —
the recording sink — fed by a **preallocated capture-slab pool** the worker
fills on the request path (Forensic mode, armed requests only). The completion
cell already reserves `FLAG_FORENSIC_ARMED`, `FLAG_BODY_TRUNCATED`, and the
`CAPTURE_POOL_FULL` / `BODY_TRUNCATED` loss reasons; `Mode.FORENSIC` and the
`capture_slabs` / `slab_bytes` budget already validate in `TelemetryConfig`.
Nothing captures yet — this stage makes those reservations real.

## Design invariants (carried from Stage-2 §7–§8)

1. **Deny by default.** No field is ever captured unless a compiled policy rule
   produces it. The native core captures nothing on its own; it is a *mechanism*
   (bounded slab, truncation, keyed hash, loss counting) that a policy drives.
2. **Redact before retention, not before export.** Hashing / masking / length-
   only reduction happens in C *as the byte is written into the slab*. Raw bytes
   for a disallowed field never exist in recorder memory.
3. **Bounded by construction.** Fixed slab count × fixed slab bytes, computed and
   validated at startup. Exhaustion truncates or drops and increments exactly one
   categorized loss counter — never allocates, never blocks the request path.
4. **A runtime arm cannot exceed the startup ceiling.** The compiled redaction
   policy is the ceiling; Inspector arm/disarm selects *among or below* it.
5. **Never write files from request code.** The sink copies committed slabs and
   writes `WFR1` asynchronously; disk-full/write failure drops output + counts,
   and never touches application work.
6. **Off/Pulse/Detailed pay nothing.** Capture is a single predicted branch
   (`ctx->capture_slot < 0` / not Forensic-armed) on every non-Forensic path.

## Slice plan

### Slice 5a — native capture-slab core ✅ *(landed 2026-07-19)*

The heart, and the hardest native invariant. No policy, no file, no Inspector —
just the bounded, redacting, lock-free slab mechanism, exercised directly through
the `Recorder`/`_Request` handle and a byte-exact pure oracle, exactly as the
phase-scratch pool was landed in Stage 3 slice 2a before anything drove it.

**Schema (both mirrors).** `_flight_schema.py` + `flight_schema.h` gain:
`EventKind.CAPTURE`; `CaptureFieldClass` (request/response header, request/
response body, query param, DB param/row, outbound request/response);
`CaptureDisposition` (`RAW`, `HASHED`, `MASKED`, `LENGTH`); a 24-byte
self-identifying **capture-slab header** (`request_id`, `used_bytes`,
`field_count`, `worker_id`, `flags`) and a 12-byte **capture-field header**
(`field_class`, `descriptor_id`, `disposition`, `stored_length`,
`original_length`) with 4-byte record alignment. A `CaptureSlab` codec decodes a
slab for the reader/oracle.

**Native core (`flight.c`).**
- A preallocated slab pool: `capture_capacity` blocks of `slab_bytes`, a
  writer-owned free stack, and **two SPSC index rings** — a *commit ring*
  (writer→sink: slabs ready to serialize) and a *return ring* (sink→writer:
  slabs the sink has copied out and released). `capture_reserve` first drains
  the return ring back onto the free stack, then pops — so the free stack stays
  single-writer-owned, no lock, mirroring the ring discipline.
- **Lazy reservation**: a Forensic-armed request reserves a slab only on its
  *first* captured field, so an armed request that captures nothing costs no
  slab. Reserve failure counts `CAPTURE_POOL_FULL` once and leaves the request
  captureless.
- `wreath_nfr_context_capture(field_class, descriptor_id, disposition, data,
  len)`: enforces the disposition (`RAW` copies up to the slab's remaining room,
  truncating with `BODY_TRUNCATED`; `HASHED` writes an 8-byte keyed hash and
  never the bytes; `MASKED`/`LENGTH` store only the original length), advances
  the slab cursor, and counts the field. A field header that will not fit is
  dropped (`CAPTURE_POOL_FULL`).
- **Keyed redaction hash**: SipHash-2-4, seeded per worker from the OS CSPRNG at
  creation (overridable via `capture_hash_key` for reproducible tests). This is
  the process-local keyed hash for correlation-without-disclosure.
- `capture_finish` commits behind a published completion (pushes the slab index
  onto the commit ring) exactly like `phase_finish`, so a dropped completion
  drops its slab; abandon/no-summary releases the slab without committing.
- Pressure gauges (`capture_in_use`, `capture_high_water`, `capture_committed`),
  relaxed atomics like the ring/phase gauges.

**Surface (`_flightmodule.c`).** `_Request.capture(...)`;
`Recorder.drain_captures(max)` (sink/test side: pops committed slabs, copies each
slab's `used_bytes`, returns the index via the return ring);
`capture_capacity` / `capture_in_use` / `capture_high_water` /
`capture_committed` getters; `Recorder(capture_slabs=, slab_bytes=,
capture_hash_key=)`. The `_flight` capsule exposes `context_capture` in its
vtable; the server extensions resolve it and are rebuilt in lockstep.

**Oracle + tests.** `PureRecorder` mirrors the slab pool, SipHash, truncation,
and commit-behind-completion byte-for-byte; `tests/test_flight_capture.py` drives
identical sequences through native and pure and asserts identical drained slabs,
gauges, and loss counters, plus deny-by-default (non-Forensic modes capture
nothing), slab exhaustion, per-field truncation, and hash-not-plaintext canaries.

**Deferred to later slices:** the policy that decides field class/disposition
(5b), the `WFR1` file and sink (5c), and the arm/disarm control surface (5d).

### Slice 5b — redaction policy compilation (`wreath.recording`) ✅ *(landed 2026-07-19)*

Deny-by-default `RedactionPolicy` / `CapturePolicy` / `CaptureBudget` value types.
Compile header rules (allowlist / drop / hash / constant-mask) to lower-case
header IDs; body rules to (route, direction, media-type) → max-bytes +
truncation; DB/outbound rules keyed by descriptor. `RedactionPolicy.deny_by_default()`
is the startup ceiling; a never-capture set (Authorization, Cookie, Set-Cookie,
Proxy-Authorization, DSNs, SQL parameters, …) cannot be overridden. Layered
compile (`server → app → router → route → plan`) may only *narrow*. Output is an
immutable compiled table the native core consults per captured field.

### Slice 5c — `WFR1` writer/reader + async recording sink ✅ *(landed 2026-07-19)*

Extend the container (`_pure/flight.py`, today `WFR0`: header + META + EVNT) to
`WFR1`: build/platform IDs, clock calibration, recording UUID, and typed
**capture chunks** referencing capture descriptors; footer/index on clean close,
checksummed-chunk recovery after abrupt termination. An async `RecordingSink`
(sibling of the Stage-4 `ExportPipeline`) drains committed slabs off the
projector thread, writes `WFR1` to an owner-only file, and drops+counts on
disk-full / write failure without ever touching request work.

### Slice 5d — Inspector capture control + `wreath capture` CLI ✅ *(landed 2026-07-19)*

`ARM_CAPTURE` / `DISARM_CAPTURE` / `CAPTURE_STATUS` Inspector commands behind a
capability token (the first *mutating* commands), advertised only when both an
`ArmRegistry` and an `InspectorConfig.capture_token` are configured. Every
capture command requires the token (`hmac.compare_digest`), separate from
read-only access. `ArmRegistry` (in `wreath.recording`) refuses any arm the
compiled `RecordingPolicy` ceiling does not permit, requires a positive expiry
(no forever-arm), caps concurrent arms, and prunes expired / match-exhausted arms
lazily; it is lock-free, event-loop-owned, and its `note_match` is the seam's
future hook. `wreath capture arm|status|disarm` CLI (token via `--token` or
`WREATH_CAPTURE_TOKEN`). Tests: `tests/test_flight_capture_control.py`
(token gate, ceiling enforcement, expiry, max-matches, concurrent-arm cap, arm/
status/disarm round trip, CLI).

### Slice 5e — capture made live (server + request-path seam)

**Lifecycle half ✅ *(landed 2026-07-19)*.** The server now creates a Forensic
recorder *with its capture pool* (`_create_recorder` passes `capture_slabs`/
`slab_bytes` under Forensic), and `_create_recording` builds the `RecordingSink`
(when `ServerConfig.recording_path` is set) and the `ArmRegistry` (from
`ServerConfig.recording`, the `RecordingPolicy` ceiling), started in `_start` and
stopped off-loop in `_stop_projection` beside the projector; the registry + the
inspector's `capture_token` flow into `serve_inspector`. A loopback lifecycle test
proves the sink writes a clean `WFR1` file on shutdown and capture control works
over the live socket.

**Request-path seam ✅ *(landed 2026-07-19)*.** The native
`_RequestContext._flight_capture(field_class, descriptor_id, disposition, data)`
method (mirrors `_flight_phase`: gated on a live borrowed context, deny-by-default
in the native core, inert after the escape-safe sever) plus the Python dispatch
seam (`Wreath._capture_request`): reached only when the native context reports
`flight == 2` *and* a capture plan is installed (Forensic only), it captures
request headers per the compiled plan when an `ArmRegistry` arm is active,
counting one match per active arm. Verified end-to-end over loopback
(`tests/test_flight_capture_live.py`: an armed request's allowlisted header is
persisted verbatim to the `WFR1` file, a hashed header stores only its digest,
forbidden/unlisted headers never appear; an unarmed request captures nothing).
Acceptance met: `wreath-request-trace --check` reports **no crossings added** for
Off/Pulse/Detailed, and the server seam is ASan/UBSan-clean.

*Follow-ons (mechanism already supports them; the seam wires headers first):*
request/response **body** capture (needs the async body read + streaming
fallback), response-header / DB-param / outbound capture seams, per-arm redaction
*narrowing* (the seam currently captures per the startup ceiling plan and uses the
arm set purely as the on/off + budget gate), and the HTTP/2 / HTTP/3 dict-scope
capture paths.

## Acceptance (Stage-3 §, restated for Stage 5)

- Secret canaries (credentials, headers, cookies, bodies, SQL parameters, rows,
  DSNs, outbound traffic) never appear in a slab or a `WFR1` file.
- Memory stays inside the computed fixed budget; all truncation/drop/pressure is
  visible as categorized counters and gauges.
- Runtime arms cannot broaden the startup policy.
- Off/Pulse/Detailed request-path cost and `wreath-request-trace` crossings are
  unchanged (capture is one predicted branch away from all of them).
