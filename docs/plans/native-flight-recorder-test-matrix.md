# Native Flight Recorder — behavior-proving test matrix

**Status:** planning document. This enumerates the full suite of tests that prove
the Native Flight Recorder's *behaviors and invariants*, across the built stages
(0–4) and the planned ones (5–8, including replay fault injection from
`native-flight-recorder-stage-2.md` §7). It is the checklist a reviewer uses to
answer "is this subsystem's behavior actually proven?" rather than "does it have
some tests." Boxes marked `[x]` exist today; `[ ]` are owed by the stage that
builds the surface.

The organizing principle: a test earns its place by pinning a *behavior or
invariant a user or operator relies on*, not by touching a line. Every dropped
telemetry item must be provably categorized; every disabled path must be provably
free; every cross-thread hand-off must be provably safe; every failure must be
provably isolated.

## 0. Test kinds and how they run

- **Unit** — one function/dataclass in isolation (schema codecs, mixers,
  bucketers). Fast, deterministic, no native build required where pure.
- **Differential** — drive the *same* sequence through the native `Recorder` and
  the pure `PureRecorder` oracle and assert byte-identical drained cells and
  counters. This is the parity spine; it is how the C and Python stay honest.
- **Integration** — a real recorder / real projector / real loopback server
  driven end to end (`tests/test_flight_server_lifecycle.py` is the template).
- **Property / fuzz** — randomized inputs against an invariant (traceparent
  corpus, cell corruption, frame fragmentation, fault schedules).
- **Concurrency** — the writer thread (request path) against the reader thread
  (projector/inspector) under contention; race and teardown ordering.
- **Sanitizer** — ASan/UBSan over the C paths a change touches; the `_flight`
  extension is hand-built with `-fsanitize=address,undefined` and driven via
  `importlib`.
- **Acceptance / performance** — the disabled-cost and Pulse-cost gates
  (`benchmarks/bench_flight_recorder.py`, pinned, A/A noise floor first).

Gate command sketch: `uv run pytest -k flight`, plus Ruff, ty, the request-trace
crossing check, and the sanitizer/fuzz/perf gates when the relevant surface
changes.

## 1. Cross-cutting properties (every stage upholds these)

- [x] **Disabled cost is zero.** Off mode adds no Python/native crossing
      (`wreath-request-trace` identical) and no measurable time (A/A). *(request
      trace + bench, stages 1–3.)*
- [x] **Pulse stays crossing-identical to stage 2** as later stages land. *(the
      Pulse cell is byte-identical; differential tests assert it.)*
- [ ] **Loss accounting is total.** For every subsystem, a forced drop increments
      exactly one categorized counter and no item vanishes silently. *(partial:
      ring/phase/active + projector loss proven; capture/export-queue owed by 5.)*
- [ ] **Bounded memory.** Every buffer has a cap; overflow evicts+counts rather
      than grows. High-water stays within the computed fixed budget. *(projector
      windows proven; capture slabs owed by 5.)*
- [ ] **Failure isolation.** A failing exporter/projector/sink/adapter causes
      drops only — never a request-path latency effect, exception, or stall.
      *(export-hook + pipeline isolation proven; end-to-end p99 gate owed.)*
- [ ] **No stale cross-extension pointer.** Escaped bindings after `context_end`
      are inert; teardown order never lets a reader touch freed worker state.
      *(sever contract proven for markers; replay/capture owed.)*

## 2. Schema and codecs (stage 0)

- [x] Round-trip encode/decode for completion, correlation, phase-record, and
      phase-batch cells at `CELL_SIZE`.
- [x] Reject wrong schema version, wrong kind byte, short buffers, and
      out-of-range phase counts.
- [x] `histogram_bucket` boundaries (≤1µs → 0; power-of-two edges; clamp at 63).
- [x] Metadata image canonical bytes are stable across registration order and
      change on semantic change; short vs full hash.
- [ ] Fuzz: random 64-byte buffers never crash a decoder; unknown kinds are
      skipped, malformed known kinds raise `SchemaError`. *(projector ingest
      covers the skip/decode-error split; a dedicated decoder fuzz is owed.)*

## 3. Native core: ring, active table, counters (stage 1)

- [x] Ring wrap, sequence continuity, full-drop accounting, single-writer/
      single-reader memory ordering.
- [x] Active-slot allocation, generation/reuse, capacity, snapshot, cancellation,
      teardown.
- [x] Fake-transport completion cells for success/error/cancel/disconnect/
      timeout/streaming across every built protocol.
- [x] Differential parity: native vs pure drained bytes + counters under ring and
      pool pressure.
- [ ] Free-threaded build + ASan/UBSan over the full lifecycle (re-run each stage
      that adds a C path).
- [ ] Undrained/full ring and absent/dead reader have no request-latency effect
      beyond noise. *(behavioral drop proven; the latency gate is a bench item.)*

## 4. Propagation and attribution (stage 2)

- [x] Strict W3C traceparent: valid parse, and rejection of short/long, all-zero
      trace, all-zero parent, uppercase, non-hex, `ff` version.
- [x] Native/pure `parse_traceparent` agree on a corpus.
- [x] Propagated request emits a correlation cell; unpropagated does not;
      malformed is dropped without reflecting bytes.
- [x] Route/plan attribution on HTTP/1, HTTP/2, HTTP/3, and WebSocket; `unknown`
      for unresolvable.
- [ ] Property: fuzz the 55-byte traceparent space; no crash, parity preserved.

## 5. Phases (stage 3)

- [x] Arming is deterministic in the request id (reproducible, doesn't perturb
      span ids); Pulse never arms.
- [x] Phase scratch: reserve/finish, budget and pool exhaustion counted, batch
      cell memset on reuse (no stale trailing records).
- [x] Slow/error promotion flags ride the completion cell; Pulse never promotes.
- [x] Pressure gauges (`phase_capacity/in_use/high_water`) track reserve/release.
- [x] Marker seams (auth/handler/serialize, pg pool+query, http-client) bind only
      when armed; escaped `_flight_phase` bindings are inert no-ops.
- [ ] Differential parity for phases under ring+pool pressure at rates 0/0.1/1/100%.

## 6. Projector (stage 4a) — **built, extend**

- [x] Completion-only settles after a quiet cycle; correlation+phases join in
      order; reordered tail (corr before completion) settles immediately.
- [x] Tail split across a later cycle and across a `max_cells` boundary still
      joins; nothing is lost.
- [x] Orphan correlation/phase (dropped head) counted, not emitted; pending
      overflow evicts oldest and counts it; corrupt cell → decode-error count.
- [x] Failures retained separately (non-OK / 5xx / promoted); route metrics
      aggregate count/errors/duration+buckets; recent/route windows bounded.
- [x] End-to-end over a real native Detailed/Pulse recorder.
- [x] **Concurrency:** the drain thread running while `snapshot()` is called
      repeatedly from three reader threads never tears a snapshot or deadlocks;
      assembled stays monotonic per reader. *(test_flight_projector_stress.py)*
- [x] **Property:** 25 seeds of random interleavings of {completion, correlation,
      phase, drop} over up to 60 request ids reassemble to exactly the
      completions that had a completion cell, every trailing cell joined, the
      rest categorized as orphans. *(This drove removing the fragile
      "immediate-emit on reorder" path so settling is uniformly quiet-cycle.)*
- [ ] **Soak:** feed millions of cells; assembled + all loss counters equal the
      input accounting exactly; memory stays flat. *(owed — extend the property
      test to a longer run.)*

## 7. OTLP mapping (stage 4b) — **built, extend**

- [x] Server span shape: name from route metadata, kind SERVER, hex ids, times
      from observation − duration, low-cardinality attributes only.
- [x] Failure → ERROR status; parent span id only when propagated; unpropagated
      gets deterministic non-zero synthesized ids; WebSocket naming.
- [x] Phases → child spans (CLIENT for deps else INTERNAL), dependency name
      resolved from the image, distinct child ids per sequence.
- [x] Metrics: request-count Sum (+error point) and duration ExponentialHistogram
      (scale 0, offset = first bucket); empty envelopes when nothing to export.
- [x] Full request round-trips through `json.dumps` (valid OTLP/JSON).
- [ ] **Property:** every produced request validates against the OTLP proto-JSON
      schema (optional: check against the real proto when installed). *(owed.)*
- [x] Attribute allow-list guard: every emitted attribute key is on an approved
      low-cardinality set and http.route stays a template — a fence that fails if
      a future change surfaces a concrete path/query/header/SQL/user id.
      *(test_flight_otlp.py)*

## 8. Export pipeline (stage 4c) — **built, extend**

- [x] `on_trace` enqueues; a tick exports; batching splits into `batch_size`
      requests; no snapshot provider → no metrics.
- [x] Trace/metric export failure isolated and counted; full queue drops+counts.
- [x] Background thread exports then `stop()` flushes the tail; start idempotent.
- [x] `OtlpHttpExporter` posts to `/v1/traces` and `/v1/metrics`, skips empty,
      raises on an unreachable endpoint (for the pipeline to isolate).
- [x] **Concurrency:** 4 producer threads × 500 offers while the exporter
      drains; offered == exported + dropped exactly, span_count == exported.
      *(test_flight_export.py)*
- [ ] Backpressure: a permanently-slow transport drives queue to full and holds
      there; drop count grows, memory does not. *(owed.)*

## 9. Inspector projection commands (stage 4c) — **built, extend**

- [x] Capabilities advertise TIMELINE/RECENT_FAILURES/ROUTE_DISTRIBUTIONS only
      with a projector; commands error cleanly without one.
- [x] TIMELINE newest-first + paging; RECENT_FAILURES only failures;
      ROUTE_DISTRIBUTIONS aggregates per route; CLI renders all three.
- [ ] Paging fuzz: arbitrary offset/limit never over-reads; truncated flag
      correct. *(active/metadata paging proven; extend to the new commands.)*
- [ ] The projection commands read a *consistent* snapshot while the projector
      thread mutates — assert no partial/torn row. *(owed, ties to §6 concurrency.)*

## 10. Server lifecycle (stage 4c) — **built, extend**

- [x] A running loopback server creates+starts the projector, drains its ring,
      and the Inspector reports the completions; no-telemetry → no projector;
      clean thread join on shutdown.
- [x] Startup abort (Inspector path is a non-socket file, so it fails after the
      projector starts) tears the projector down — no leaked
      `wreath-flight-projector` thread. *(test_flight_server_lifecycle.py)*
- [x] Sustained load: 60 requests are all projected (Inspector TIMELINE
      assembled ≥ 60) and shut down with zero RING_FULL loss — the projector kept
      the ring drained. *(test_flight_server_lifecycle.py)*
- [ ] OTLP-enabled server end to end against a localhost collector: spans arrive
      with correct route names. *(owed — extends the lifecycle test.)*

## 11. Lazy bridge (stage 4c) — **built, extend**

- [x] `current_span` parses incoming traceparent to an immutable view; empty for
      unpropagated/malformed/no-header; traceparent round-trips.
- [x] `activate_otel` returns the native view when the SDK is absent or the
      request is unpropagated.
- [ ] With `opentelemetry` installed (optional CI lane): `activate_otel` returns
      a Context wrapping a non-recording remote span with the right ids, and
      entering a handler alone constructs no SDK object. *(owed — needs an
      opt-in dependency lane.)*

## 12. Capture, redaction, security (stage 5)

- [ ] Secret canaries: credentials, headers, cookies, bodies, SQL params, DB
      rows, DSNs, outbound traffic never enter a slab or file.
- [ ] Structured-redaction depth/field/invalid-input limits; per-field/request/
      route/global truncation; slab exhaustion counted.
- [ ] Arm authorization + capability token, expiry, match-count, startup-ceiling
      enforcement; a runtime arm cannot broaden startup policy.
- [ ] Disk-full, sink failure, crash recovery, checksums, version rejection.
- [ ] Inspector mutation fuzz: malformed/oversized/unauthorized arm requests.

## 13. Replay + fault injection (stages 6–7)

- [x] Transport replay: golden captures for fragmentation, pipelining, flow
      control, reset, timeout, malformed input, backpressure, disconnect,
      streaming reproduce byte/semantic-equivalently; same capture repeatable.
      (`test_replay`, `test_replay_faults`, `test_replay_adversarial`; H2 in
      `tests/http2/test_replay*`.) The `timeout` fault drives the driver's own
      armed deadline enforcement — native `_replay_fire_timeout` →
      `enforce_deadline`, mirrored by the pure twin — not a simulated outcome: an
      incomplete body-awaiting request emits a real `408`, an idle connection
      closes, byte-identical on both twins.
- [x] Endpoint-plan replay: binding/validation/auth/serialization parity; DB
      result/transaction success/failure/ambiguous; outbound retry sequences;
      virtual time/random; missing/unexpected/duplicated/reordered effects.
      (`test_replay_stage7`, `test_replay_robustness`.)
- [x] **Fault injection (stage-2 §7):** one schedule per taxonomy entry
      (transport incl. timeout, scheduling, adapter, recording-reader), each
      proving a deterministic owned outcome — same recording + same schedule →
      identical terminal status, loss counters, and normalized bytes across runs
      and builds (`fault_corpus` + `test_replay_fault_corpus`). Sink-seam faults
      are covered by the stage-4/5 hardening suites
      (`test_flight_projector_stress`, `test_flight_export`,
      `test_flight_recording_format`).
- [x] Fault-schedule round-trip: a `FaultSchedule` serializes through the
      checksummed `WFS1` container and replays to the same outcome
      (`test_replay_fault_corpus` drives every corpus entry via `to_bytes` →
      `from_bytes`); a corrupt/truncated schedule is rejected, never mis-applied.
- [x] The whole fault corpus runs clean under ASan/UBSan; each modeled fault
      resolves to exactly one categorized outcome
      (`test_replay_fault_corpus -k native` under the sanitized `_server`).
- [ ] Incompatible image/version/build rejection; sanitizer/fuzzer seed
      integration; replay throughput + bounded-memory (not a production claim).

## 14. Deployment hardening (stage 8)

- [ ] Multi-worker Inspector aggregation: ordering/loss across workers, process
      crash/restart.
- [ ] Long exporter/capture pressure soak; GIL/free-threaded/JIT matrices.
- [ ] Format upgrade/downgrade corpus; control-socket abuse cases; security
      review checklist.

## 15. Immediately actionable (built surface)

These need no unbuilt stage and harden real invariants now. Landed 2026-07-19:

1. [x] Projector snapshot/drain **concurrency** stress (§6) and export-pipeline
   producer contention (§8) — the thread-safety claims are now exercised under
   contention, not just asserted by construction.
2. [x] Projector **property test** over random cell interleavings (§6) — drove
   out the fragile immediate-emit path; reassembly/loss accounting proven exact.
3. [x] OTLP **cardinality/secrecy fence** (§7).
4. [x] Server **startup-abort** (no leaked thread) and **sustained-load** drain
   (§10).

Still owed on the built surface:

5. [ ] Decoder and traceparent **fuzz** (§2, §4).
6. [ ] Projector **soak** (§6) and export **backpressure hold** (§8).
7. [ ] OTLP-enabled server against a localhost collector (§10) and the
   opentelemetry-installed `activate_otel` lane (§11).
