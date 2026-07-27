# Native Flight Recorder plan — Stage 8: deployment hardening and the shadow gate

**Status:** landed. Stages 0–7 are complete (see the Stage-3 programme checklist,
Stage-5, and the Stage-2 §7 replay checklist). Stage 8 is the final stage: it
states the deployment posture the whole subsystem was built to hold, records how
each invariant is enforced and tested, and defines the read-only shadow-execution
gate that a future runner may build behind — without a redesign.

All names use Wreath. Nothing here weakens a Stage 0–7 invariant; it documents
and locks them.

## 1. Deployment posture

The recorder is opt-in and layered Off → Pulse → Detailed → Forensic. Each layer
adds only what the operator asked for, and every layer holds these invariants:

- **No request-path cost you did not enable.** The Off, Pulse, and Detailed paths
  add zero Python↔native boundary crossings (enforced every change by
  `wreath-request-trace --check` against `docs/agents/request-boundary-baseline.json`).
  Forensic capture runs only for an armed, sampled request with an active arm.
- **Deny-by-default capture.** Headers, query parameters, bodies, DB
  parameters/rows, and outbound bodies are captured only when a policy names
  them; the never-capture set (Authorization, Cookie, API keys, …) cannot be
  enabled through any API. A runtime arm can only *narrow* the startup ceiling,
  never broaden it — proven by `RecordingPolicy.permits` and the per-arm
  narrowing tests.
- **Bounded everything.** Ring, phase-scratch pool, active table, capture-slab
  pool, export queue, and recording sink are all fixed-size; exhaustion drops and
  increments a categorized loss counter — never an unbounded allocation. The
  capacity/in-use/high-water gauges make pressure observable.
- **Redaction before retention.** The native capture core redacts (hash/mask/
  bounded-raw) *as it writes*, so forbidden bytes never exist in recorder memory
  or a file. The `WFR1` sink is the sole capture-slab consumer and writes an
  owner-only (`0600`) file off the request path; a disk/write failure degrades to
  drain-and-drop with a counter and never touches application work.
- **Capability-gated mutation.** The Inspector is a same-UID Unix-socket service
  that never binds TCP; read commands need no token, and the mutating capture
  arm/disarm commands require a separate capability token (`hmac.compare_digest`,
  ≥16 chars) and a bounded, expiring `ArmRegistry` (no forever-arms).
- **Replay/fault injection is test-only.** Replay runs over fake transports and
  injected adapters exclusively; it cannot reach a real socket, file, or
  subprocess, and cannot broaden any capture policy.

### Enforcement and test map

| Invariant | Enforced by | Proven by |
| --- | --- | --- |
| No added crossings Off/Pulse/Detailed | dispatch gating on `flight == 2` | `wreath-request-trace --check` |
| Deny-by-default + never-capture set | `RedactionPolicy`, native deny-by-default | `test_flight_recording`, `test_flight_capture*` |
| Arm ⊆ ceiling (never broadens) | `RecordingPolicy.permits`, per-arm narrowing | `test_flight_capture_live`, `http2/test_flight_capture` |
| Bounded pools, categorized loss | fixed-size native pools + counters | `test_flight_capture`, `test_flight_projector_stress`, `test_flight_export` |
| Redaction before retention | native `context_capture` | `test_flight_capture` (secret canaries), sanitizer harness |
| Owner-only file, disk-full degrade | `RecordingSink` | `test_flight_recording_format` |
| Capability-gated arm/disarm | Inspector token + `ArmRegistry` | `test_flight_capture_control` |
| Replay cannot reach real resources | fake transports + injected adapters | `test_replay*`, `test_replay_fault_corpus` |
| Memory safety under adversity | the fault corpus | `test_replay_fault_corpus -k native` under ASan/UBSan |

The **fault corpus** (`wreath.replay.fault_corpus`) is the deployment gate's
teeth: one schedule per §7 taxonomy region (transport, scheduling, adapter,
reader), each asserted to a deterministic owned outcome and re-run under the
sanitizers. The fault-injection library and the ASan/UBSan gate are the same
artifact, exactly as §7 requires.

## 2. The shadow-execution gate

Shadow execution — re-running a *copy* of live traffic through the owned pipeline
in the background to compare against production — is **deferred by design**, but
Stages 6–7 were built so it can be added behind this gate without a redesign:

- **Endpoint-plan replay is already the shadow primitive.** `replay_endpoint_plan`
  runs a `CanonicalRequest` through the owned routing/binding/validation/
  serialization with no socket and with `INVOKE`/`REPLACE`/`SKIP` handler modes.
  A shadow runner is "feed live canonical requests to it off the request path and
  diff the result" — the comparison surface (`PlanReplayResult`) and the boundary
  adapters (`ReplayAdapters`) already exist.
- **Ordering and identity are preserved.** Completion cells, correlation, phases,
  and the owned server span id (exposed via `_flight_server_span`) all carry the
  parent request identity, so a shadow run can be attributed back to the request
  it mirrors.
- **The gate.** A shadow runner MUST be read-only: it runs in `REPLACE`/`SKIP`
  mode or with every external effect supplied by an adapter, never invoking a
  side-effecting handler against a real backend, and it is labelled *best effort*
  the moment it executes arbitrary Python. No live duplicate execution with real
  side effects belongs in the initial implementation. When a shadow runner is
  built, it lands as its own stage behind this gate; nothing above changes.

## 3. Acceptance

Stage 8 is accepted when the posture in §1 is enforced and tested (it is, per the
map), the fault corpus runs clean under the sanitizers, and the shadow gate is
documented so a future runner has a designed seam. The Native Flight Recorder is
then **fully implemented**: capture across HTTP/1, HTTP/2, and HTTP/3; off-path
projection and OTLP export with drift-free span timing; the Inspector control
plane; and transport + endpoint-plan replay with a full fault-injection taxonomy —
HTTP/3 transport replay excepted by the QUIC-crypto boundary documented in the
replay reference.
