# Wreath-owned native event loop plan

## Status

Accepted implementation direction as of 2026-07-21. All seven architecture items in the 2026-07-22 review packet are implementation commitments. The previous stop/go design gates and review recommendations that narrowed, deferred indefinitely, or rejected part of those seven items are superseded by the explicit implementation mandate below.

This acceptance is not a claim that io_uring is faster than epoll, that any individual mechanism will improve end-to-end performance, or that Wreath currently beats uvloop/libuv. Measurements determine configuration, default promotion, and published claims; they do not cancel implementation of the seven committed items. ASGI correctness, bounded ownership, memory safety, security review, optional dependency boundaries, fallback behavior, and the repository's repeated-measurement rules remain non-waivable.

### Implementation progress (2026-07-22)

- HTTP/3 retained-response byte/segment credit and acknowledgement-driven ASGI send waiters are implemented in source, with focused tests; rebuilding the optional extension is currently blocked on this development host because `pkg-config` cannot locate the ngtcp2/nghttp3 toolchain.
- HTTP/2 transport writable credit gates DATA framing and ASGI send completion. Equal-priority streams use persistent-deficit DRR with bounded stream/byte work per activation. Metal now layers RFC 9218 request `Priority` and `PRIORITY_UPDATE` handling over that queue: strict urgency selection, same-urgency rotation for incremental responses, bounded sequential preference for non-incremental responses, higher-urgency preemption, and a concurrent-stream-bounded table for updates that precede HEADERS. Stock asyncio/uvloop keeps equal-priority DRR behavior.
- Metal epoll registrations now carry 64-bit `{generation, fd}` tokens, reject stale generations after same-batch replacement or descriptor reuse, and expose stale-event and generation-wrap counters. The focused regression and non-HTTP/3 reactor tests pass; the optional HTTP/3 metal test is blocked by the missing `libngtcp2_crypto_ossl.so.0` runtime library.
- Metal HTTP/1 socket operations use one loop-owned io_uring and bounded generational connection/operation slabs. Accept activates plaintext transports directly from the CQ. Fused HTTP/1 uses one-shot receive directly into parser reserve/commit storage; HTTP/2 and generic protocols retain the registered provided-buffer substrate. Immutable writes transfer into completion ownership, a bounded retained queue prevents copies behind an active send, and SEND_ZC is limited to payloads of at least 16 KiB. SQ tails, CQ heads, submissions, and blocking waits are batched with explicit telemetry. Generic readiness uses optimistic multishot poll with one-shot fallback, and eventfd wakes coalesce while pending. Stock asyncio/uvloop `wreath-native` transports do not enter this path.
- `metal_event_loop(worker_id=...)` labels one loop-owned worker domain and rejects operation submission from another OS thread. Explicit `reuse_port=True` creates metal TCP/UDP `SO_REUSEPORT` listener groups without changing stock asyncio/uvloop defaults. `wreath run --loop metal --workers N` now supervises one loop per child, uses a post-bind/post-lifespan readiness pipe, replaces unexpected exits, and handles `SIGHUP` by readying a complete replacement generation before gracefully draining the old one. Shutdown is bounded by `shutdown_timeout`, after which a stuck child is killed. Broader cross-platform process models remain outside this Linux metal path.
- No throughput or latency win is claimed from these correctness changes. No full benchmark has been run; future measurements must remain short, task-specific microbenchmarks on this thermally constrained host.

### Authoritative implementation checklist

This checklist is the definition of done for this plan. A checked box means the
implementation and focused correctness coverage exist in the repository; it does
not imply a measured performance win or default promotion. Unchecked boxes are
jobs to be done (JTBD). The dated review packet below is retained as historical
context and may describe boundaries that this checklist has since advanced.

#### Item 1 — completion demultiplexing, adaptive polling, and linked SQEs

- [x] Route metal HTTP/1 socket operations through backend-neutral operation and
  completion ownership records.
- [x] Make production `metal_event_loop()` unconditionally own accept, receive,
  send, native timers, direct poller dispatch, and adaptive polling through the
  metal implementation, with no public backend or polling-mode selection.
- [x] Remove epoll from metal's ring-fd wait and wake path by consolidating work on
  one loop-owned io_uring completion domain.
- [x] Remove synchronous/epoll setup fallbacks from production metal; failure to
  initialize required accept, provided-buffer receive, or asynchronous-send
  ownership now fails startup rather than changing execution architecture.
- [x] Implement io_uring accept, receive, and send ownership.
- [x] Implement bounded CQ draining for accept, receive, and send rings.
- [x] Gather asynchronous SQEs behind a poller-local SQ tail, publish the shared
  tail once per enter, consume bounded CQ batches behind one local head, publish
  the shared CQ head once, combine pending submission with blocking waits, and
  expose SQE, publication, batch, and enter telemetry.
- [x] Implement multishot accept and receive with independent one-shot fallback.
- [x] Implement cancellation and EOF/error/stale-completion handling for the
  current accept/receive/send operations.
- [x] Implement the GIL-free adaptive spin-then-block controller using
  empty-CQ-to-arrival EWMA and deviation, strict time clamps, variance suppression,
  and hit/miss/blocking/CPU-time telemetry.
- [x] Remove the redundant synchronous io_uring instance now that production
  receive/send ownership is completion-driven, reducing metal from four rings to
  three without reintroducing direct syscalls.
- [x] Consolidate listener accept, provided-buffer receive, asynchronous send,
  cancellation, and eventfd wake onto one 512-entry loop-owned io_uring, one
  tagged CQ demultiplexer, and one bounded submit phase.
- [x] Poll CPython signal wake bytes on the unified ring and use
  `io_uring_enter(EXT_ARG)` deadlines for native-wheel/asyncio timer waits; standard
  metal HTTP/1 operation now allocates no epoll fd and performs no epoll waits.
- [x] Route generic reader/writer readiness, including the HTTP/3 UDP datagram
  adapter, through tagged `IORING_OP_POLL_ADD` requests with generation-validated
  CQ dispatch, cancellation, optimistic multishot polling with kernel-rejection
  fallback, and bounded rearm; delete the epoll fd, event array, registration
  calls, and wait branches from metal.
- [x] Submit TCP output at or above the structurally tested 16 KiB threshold with
  `IORING_OP_SEND_ZC`, use ordinary async send below it, retain immutable payloads
  through final ownership return, handle copied completions, and expose copied,
  notification, and byte counters. Local non-TCP test sockets retain ordinary
  asynchronous send on the same CQ contract.
- [x] Route ordinary cross-thread scheduling through a native eventfd polled as an
  io_uring completion, coalesce pending producer wakes atomically, and expose
  request/write/coalesced/poll telemetry; retain the inherited socketpair only for
  CPython signal wake bytes.
- [ ] Implement linked/hard-linked SQE prefixes using reserved
  fixed/direct descriptor slots, including short-read and link-cancellation tests.
- [x] Add opt-in bounded completion-trace capture for HTTP/1 receive/send,
  cancellation, EOF, and timed-wait lifecycles; the production default allocates
  no trace ring and performs no completion-path trace writes. The obsolete internal epoll
  differential was removed with the epoll implementation.
- [ ] Extend differential traces across cancellation, timeout, EOF, partial send,
  stale completion, pool exhaustion, and shutdown fault cases.

#### Item 2 — per-core generational slabs

- [x] Implement bounded per-worker connection and operation slabs with 64-bit
  generation tokens, O(1) free lists, generation bumps, and no cross-worker table.
- [x] Validate operation incarnation before referenced connection state on async
  send completions.
- [x] Reject stale epoll events and stale accept/receive/send CQEs.
- [x] Return selected receive buffers on success and stale receive completion.
- [x] Expose occupancy, exhaustion, high-water, stale, and generation-wrap
  diagnostics for the implemented slabs.
- [ ] Move protocol stream records into a distinct per-worker generational slab.
- [x] Move provided-buffer ownership into a distinct generational descriptor
  slab, validate kernel buffer IDs against the live ownership epoch before use,
  and expose occupancy, exhaustion, stale-token, and wrap diagnostics.
- [x] Add validated startup sizing for connection, operation, and provided-buffer
  descriptor arenas so capacity/RSS is fixed before serving; affinity pinning runs
  before those allocations for worker-local first touch.
- [ ] Add sanitizer and fault-injection coverage for delayed CQEs, slot reuse,
  descriptor reuse, multishot termination, and generation wrap.

#### Item 3 — bip buffers over provided-buffer rings

- [x] Register a bounded per-worker io_uring provided-buffer ring for metal TCP
  receive and expose explicit setup fallback.
- [x] Recycle selected buffers after protocol delivery, cancellation races, and
  stale completions.
- [x] Stop/cancel and rearm receive across `pause_reading()`/`resume_reading()`.
- [x] Submit fused HTTP/1 one-shot receives directly into parser-owned
  reserve/commit storage, with operation-slab lifetime pinning and cancellation
  cleanup; provided buffers remain the shared HTTP/2 and generic ingress substrate.
- [ ] Implement per-connection bip reserve/commit/consume regions.
- [ ] Add segmented parser input for records spanning bip reservations.
- [ ] Add bounded scratch linearization for split fixed-size structures such as
  HTTP/2 frame headers.
- [ ] Remove mandatory provided-buffer-to-parser staging copies for HTTP/1 and
  HTTP/2 metal ingress.
- [ ] Put metal HTTP/3 datagrams on pool-backed one-buffer-per-datagram ownership.
- [ ] Define and test explicit pool-exhaustion backpressure/rejection behavior.
- [ ] Prove every selected buffer is returned exactly once across success,
  cancellation, close, fallback, and stale-operation paths.

#### Item 4 — per-core affinity, server CIDs, and eBPF steering

- [x] Give each metal loop an explicit worker identity and owner-thread checks.
- [x] Support explicitly selected TCP/UDP `SO_REUSEPORT` listener groups.
- [x] Implement `--workers` process supervision, post-startup readiness pipes,
  unexpected-exit replacement, bounded shutdown, and ready-before-drain `SIGHUP`
  generation replacement.
- [x] Add opt-in deterministic worker CPU affinity before loop/ring/slab
  allocation so native worker-owned memory is first-touched after pinning.
- [ ] Add explicit NUMA-node placement and cross-node diagnostics beyond the
  affinity-driven first-touch baseline.
- [ ] Replace development QUIC CID randomness/static secrets with a reviewed
  server CID key and codec.
- [ ] Encode authenticated or blinded worker routing in Retry and subsequent
  server-issued QUIC CIDs.
- [ ] Implement bounded long-/short-header CID extraction for reuse-port steering.
- [ ] Implement and capability-gate `SO_ATTACH_REUSEPORT_EBPF` steering.
- [ ] Handle migration, NAT rebinding, invalid routes, key rotation, worker-count
  changes, and draining generations without shared connection state.
- [ ] Verify privileged and unprivileged fallback behavior for HTTP/1–3 workers.

#### Item 5 — unified stream layer, DRR, and RFC 9218

- [x] Couple HTTP/2 DATA framing to stream, connection, and transport writable
  credit.
- [x] Implement persistent-deficit DRR with bounded stream/byte work.
- [x] Implement metal RFC 9218 request `Priority` and `PRIORITY_UPDATE` handling,
  strict urgency, incremental rotation, non-incremental sequential preference,
  higher-urgency preemption, and bounded idle-stream updates.
- [ ] Define one internal stream event/ownership contract for headers, body,
  trailers, reset/disconnect, flow credit, output segments, priority, and upgrade.
- [ ] Drive HTTP/1, HTTP/2, HTTP/3, and WebSocket frontends through that contract.
- [ ] Replace protocol-specific duplicate application activation/suspension and
  ownership transitions with the shared driver while retaining wire-specific state.
- [ ] Integrate HTTP/3 urgency/incremental policy through nghttp3 priority hooks.
- [ ] Prove normalized queues remain bounded under slow applications and peers.

#### Item 6 — SWAR/SIMD parsing and Wreath-owned QPACK blocking policy

- [ ] Isolate canonical scalar delimiter/classification scanners behind a common
  parser interface.
- [ ] Implement runtime-dispatched SWAR scanners for request-line and header parsing.
- [ ] Implement available architecture-specific SIMD arms with runtime CPU dispatch.
- [ ] Add differential malformed/incremental/page-edge tests across scalar,
  SWAR, and SIMD implementations.
- [ ] Preserve resumable linear scanning without rescans in every arm.
- [x] Configure nghttp3 dynamic-table and blocked-stream limits.
- [ ] Track blocked HTTP/3 streams and retained Wreath memory per connection.
- [ ] Expose required-insert-count/known-received progress where nghttp3 permits.
- [ ] Add timeout, reset, close, cancellation, and excessive-load policy for QPACK
  blocked streams.
- [ ] Prove a stalled encoder stream cannot retain unbounded application state.

#### Item 7 — unified end-to-end credit coupling

- [x] Bound HTTP/3 retained response bytes and segments and release ASGI send
  waiters from acknowledgement-driven ownership return.
- [x] Gate HTTP/2 output on transport pressure and bounded scheduler activation.
- [x] Cancel/rearm metal receive from transport read demand while recycling racing
  selected buffers.
- [x] Count in-flight asynchronous send bytes in metal high/low-water flow control
  and delay graceful close until accepted bytes complete.
- [ ] Define one explicit credit record/model spanning ASGI demand, normalized
  stream queues, protocol windows, receive posting, send acceptance, and drain.
- [ ] Make HTTP/1–3 and WebSocket ingress posting derive from application demand
  and bounded queue/message credit through the shared stream driver.
- [ ] Make every outbound scheduler require application, protocol, connection,
  transport, segment, and retransmission credit as applicable.
- [ ] Audit reset, disconnect, cancellation, timeout, and shutdown so every credit
  and ownership unit is returned or invalidated exactly once.
- [ ] Add slow-app, slow-upload, stalled-reader, stalled-peer, reset-race, and
  shutdown pressure tests proving configured memory bounds at every layer.

#### Cross-cutting protocol-driver and verification JTBD

- [ ] Introduce the transport-neutral protocol driver for ingress segments,
  writable/output ownership, timers, close/reset/disconnect, application
  activation/suspension, and pressure return.
- [ ] Keep asyncio/uvloop as the reference adapter and drive the same contract
  directly from metal without protocol-semantic forks.
- [ ] Rebuild and run optional HTTP/3 coverage once ngtcp2/nghttp3 and
  `libngtcp2_crypto_ossl.so.0` are available on the development host.
- [x] Add deterministic metal-owned mmap/heap capacity, io_uring-count,
  inherited-selector, SQE-per-enter, blocking-entry, and one-request receive/send
  submission baselines, plus close-time mapping release, diagnostic-memory, and
  tuned-arena scaling gates.
- [ ] Run sanitizer, fuzz, fault-injection, and soak coverage required by the seven
  completion definitions.
- [ ] Run only short, task-specific before/after microbenchmarks on this thermally
  constrained host; retain raw results and do not promote defaults from one run.
- [ ] Record final epoll/io_uring parity, ASGI conformance, memory-bound, worker
  migration, parser differential, and QPACK lifecycle evidence in this plan.

### Operator microbenchmark (not run by this implementation work)

After correctness work, run one measured plaintext pass of 100 requests per Wreath
execution mode, preceded by ten warm-up requests. Concurrency one keeps this a
small framework/loop comparison rather than a sustained load test:

```console
uv run wreath-bench --framework wreath wreath-native wreath-metal --scenario plaintext --requests 100 --warmup-requests 10 --concurrency 1
```

The command records all three results through the existing benchmark runner. It
is a development microbenchmark, not publishable evidence and not sufficient by
itself to promote a backend default.

## Goal

Determine whether a Wreath-specific C event loop can outperform uvloop/libuv by owning the server socket, readiness/completion processing, protocol ingress, timers, response writes, and handler activation as one measured system. Preserve Wreath as a conforming ASGI framework and retain asyncio/uvloop as supported server backends.

The intended advantage is specialization and removal of transport/scheduling boundaries—not replacing one mature C poll call with another and assuming it is faster.

## Current repository boundary

Wreath's native server is already below Python for parsing, framing, connection state, HTTP/1 response serialization, HTTP/2 stream/flow-control state, WebSockets, and optional HTTP/3. It still runs over an asyncio or uvloop transport:

```text
kernel
  -> asyncio selector or libuv
  -> Python transport/protocol callback
  -> wreath._native._server protocol state
  -> Wreath application / handler activation
  -> Python transport.write() or writelines()
  -> asyncio selector or libuv
  -> kernel
```

Concrete seams:

- `src/wreath/server.py` creates TCP listeners with `loop.create_server()`, UDP/HTTP-3 listeners with `loop.create_datagram_endpoint()`, accepts an explicit `loop_factory`, and runs through `asyncio.run()`.
- `src/wreath/_native/server.h` stores the loop, transport, bound transport write functions, task/future factories, timer callbacks, request state, and reusable protocol buffers.
- `server_http1.c` calls Python transport `write()`/`writelines()` even when parsing and response building are native.
- HTTP/1, HTTP/2, and HTTP/3 create or schedule Python tasks through loop methods when an application coroutine suspends.
- `benchmarks/wreath_server.py` already compares `asyncio` and `uvloop`; the main benchmark harness records loop/server metadata and supports independent protocol-capable load generation.
- `wreath-request-trace`, `wreath-tape-decomp`, `wreath-decomp`, and `src/wreath/_devtools/measure.py` provide exact boundary counts, ablation-oriented measurements, A/A noise floors, and lifecycle decomposition.

The first target is therefore the transport and scheduler seam already visible in these files. Do not redesign routing, binding, ORM, telemetry, or protocol semantics to make the loop easier.

## Review packet for 2026-07-22: current native/metal reconciliation

This section records the 2026-07-21 read-only review of the current `wreath-native` and `wreath-metal` implementation against the proposed completion-reactor, lifetime, buffering, multicore, scheduling, parsing, compression, and backpressure design. No files were mutated during the review, and no tests or benchmarks were run. Every performance effect below is therefore a code-derived hypothesis, not a measured claim.

The implementation has advanced beyond parts of the “Current repository boundary” above, but it has not reached the completion architecture described later in this plan:

- **`wreath-native`** runs Wreath's native HTTP protocol implementations over asyncio or uvloop transports.
- **`wreath-metal`** runs those protocols on an asyncio-compatible `SelectorEventLoop` subclass with a C `epoll` poller, a C TCP `SocketTransport`, a native timing wheel, and direct dispatch for selected CPython task-step callbacks.
- Metal still creates listeners through `loop.create_server()`, retains Python protocol/transport objects, and performs synchronous `recv()`/`send()` calls from readiness callbacks. It is not yet an SQE/CQE reactor and does not own accept as a completion operation.
- The strongest fusion is currently the plaintext HTTP/1 path. HTTP/2 does not use the HTTP/1 transport C API, and HTTP/3 still enters through an asyncio datagram transport.
- Metal currently activates ASGI coroutines through CPython's task implementation after native routing and protocol ownership. Replacing that remaining activation/suspension seam with the shared native stream driver is committed JTBD, not a runtime mode.

This creates two implementation tracks. Stable protocol, pressure, fairness, and adapter improvements belong to Wreath's server as a whole and must benefit every supported loop. Completion-only polling, kernel programming, fixed arenas, and instruction-set-specific work belongs to the `wreath-metal` implementation. It is not promoted into the separate asyncio/uvloop-backed `wreath-native` path.

### Explicit implementation mandate: all seven items

This mandate overrides the earlier design gates and every intentional narrowing in the read-only review. “Deferred” means sequenced later, not optional or rejected. Measurements tune budgets and implementation details; they do not turn committed metal ownership back into a runtime backend choice. If a target kernel cannot provide a required semantic, fail clearly and document the constraint rather than silently changing architecture.

The isolation rule is:

- **Wreath-wide:** protocol semantics, stream normalization, scheduling, pressure, portable parser interfaces, worker lifecycle, configuration, and asyncio/uvloop adapters.
- **`wreath-metal`:** io_uring completion ownership, adaptive polling, linked/direct SQEs, slab allocation, provided-buffer rings, NUMA placement, eBPF steering, zero-copy send, fixed descriptors, and SIMD/SWAR dispatch. These are the metal architecture, not candidates for promotion into `wreath-native`.

#### Item 1 — completion demultiplexing with adaptive polling and linked SQEs

**Commitment:** implement a loop-owned submission/completion core in `wreath-metal`, one ownership domain per worker/core. Protocol handlers emit operations and consume completion-driven state transitions; they do not issue socket syscalls directly on the metal fast lane. Each bounded loop iteration drains completions, validates ownership, dispatches state machines, collects resulting submissions, and performs one batched submit when work exists.

Required arms:

- direct epoll adapted into the same completion record model;
- ordinary io_uring accept/recv/send;
- batched SQ submission and CQ drain;
- multishot accept and receive where supported;
- adaptive userspace spin-then-block;
- linked or hard-linked SQE graphs where the kernel can express a valid dependency;
- explicit cancellation, timeout, wake, EOF, error, and notification completions.

The adaptive controller is required, not merely considered. It must learn empty-CQ-to-arrival behavior, maintain EWMA location and dispersion, suppress spin under high variance, and clamp spin by time/CPU policy. Blocking remains available, and the controller must expose spin duration, hit/miss rate, blocking enters, and CPU cost.

The linked-SQE requirement overrides the earlier recommendation to omit it. Because an ordinary accept result cannot become the next SQE's fd, the implementation must use an explicitly reserved direct/fixed descriptor slot or another kernel-supported handoff. Short-read and link-cancellation semantics require dedicated tests. If a protocol sequence is data-dependent, only its fixed valid prefix is linked; state-machine work resumes from its completion.

**Complete when:** the unified io_uring completion domain covers I/O, wakeups, and timers; adaptive polling and linked graphs have forced differential-test controls but one production behavior; unsupported required kernel capabilities fail startup; and no protocol embeds syscall-specific ownership.

#### Item 2 — per-core generational slabs

**Commitment:** implement per-worker contiguous arenas for operation records, connections, streams, and buffer descriptors on the metal fast lane. Native state is addressed through generation-validated integer handles rather than raw pointers crossing asynchronous ownership boundaries.

The original `{index:24, generation:8}` concept is retained in purpose but strengthened to a 64-bit token because an 8-bit generation wraps under realistic churn. Operation handles and connection/stream handles are distinct: a CQE validates its operation incarnation first, releases backend resources even when stale, then validates the referenced state object before dispatch.

Required behavior:

- O(1) allocate/free from bounded or startup-grown per-worker slabs;
- generation bump on every free;
- stale epoll event and stale CQE rejection;
- correct multishot lifetime until `MORE` clears;
- selected-buffer and zero-copy-notification release even for stale operations;
- occupancy, exhaustion, high-water, generation-wrap, and stale-event counters;
- no hidden cross-worker mutable table.

**Complete when:** the existing epoll path uses generation tokens, io_uring `user_data` carries operation handles, connection/stream churn cannot dispatch into a reused incarnation, and sanitizer/fault-injection coverage exercises delayed completions and descriptor reuse.

#### Item 3 — bip buffers over provided-buffer rings

**Commitment:** implement per-worker receive pools and io_uring provided-buffer rings in metal, plus bip-buffer-based contiguous reservation for per-connection TCP ingress where it is applicable. NUMA-local first-touch/allocation is required once workers are pinned.

The earlier caveat remains an engineering constraint, not a narrowing: a bip buffer guarantees a contiguous producer reservation but cannot guarantee that a protocol record split across multiple receives is contiguous. Therefore the full implementation includes:

- provided-buffer IDs with explicit retain/release ownership;
- bip reserve/commit/consume regions for per-connection stream ingress;
- segmented parser input when a record spans reservations;
- tiny bounded scratch linearization for structures such as an HTTP/2 frame header when preferable;
- one selected contiguous buffer per QUIC datagram;
- pool exhaustion behavior that applies backpressure or rejects explicitly rather than allocating without bound;
- epoll and io_uring adapters over the same buffer-ownership interface.

This item is not satisfied solely by converting HTTP/2 to `BufferedProtocol`; that is an integration step toward the final pool/bip design.

**Complete when:** HTTP/1 and HTTP/2 consume pool-backed ingress without mandatory socket-to-parser staging copies, HTTP/3 datagrams use pool-backed receive on metal, wrap/split cases preserve protocol parity, and every selected buffer is returned exactly once through success, cancellation, close, and stale completion.

#### Item 4 — per-core affinity with server CIDs and eBPF steering

**Commitment:** implement an explicit worker-per-core/process ownership model, `SO_REUSEPORT` listener groups, server-issued QUIC connection IDs carrying authenticated or blinded worker routing, and `SO_ATTACH_REUSEPORT_EBPF` steering in metal.

Wreath-wide work provides worker lifecycle, process ownership on normal GIL CPython, reuseport configuration, graceful restart, and non-BPF fallback. Metal owns the eBPF program, secure CID codec, capability probing, and affinity/pinning integration.

Required behavior:

- Initial client-selected QUIC CIDs use normal initial distribution.
- Retry CIDs and all subsequent server-issued CIDs encode the owner.
- Long- and short-header parsing is bounded and compatible with the fixed CID format.
- NAT rebinding and connection migration land on the owning worker.
- CID authentication prevents arbitrary worker-selection tampering.
- Key rotation, worker-count changes, draining workers, invalid routes, and restart are specified.
- The current development `rand()` and static secret are replaced before production enablement.
- TCP remains naturally pinned after accept; a TCP BPF selector is implemented only for initial distribution if needed, not under the false premise that established keep-alives require resteering.

**Complete when:** multiworker HTTP/1-3 operate through reuseport, QUIC migration reaches the original owner without shared connection state, privileged/unprivileged startup behavior is explicit, and the non-BPF path remains correct.

#### Item 5 — unified stream layer with DRR and RFC 9218

**Commitment:** implement one internal stream event contract covering headers, body chunks, trailers, reset/disconnect, flow credit, writable credit, response segments, priority, and upgraded WebSocket state. HTTP/1, HTTP/2, HTTP/3, and WebSocket frontends normalize into that contract before common application dispatch.

This overrides the earlier recommendation to avoid a mandatory normalization queue. The queue must nevertheless be bounded and allocation-conscious: use intrusive/per-stream records, fuse immediate single-event activation where semantics permit, and preserve exact event ordering. ASGI continues to expose `scope["http_version"]` and distinct WebSocket messages; normalization does not erase required public semantics.

Outbound scheduling requirements:

- DRR active queues with persistent deficits and bounded work per activation;
- connection and stream flow-window gating;
- transport/socket writable-credit gating;
- RFC 9218 urgency bands;
- `incremental` interleaving within a band;
- non-incremental sequential preference without blocking higher urgency/control work;
- abuse limits for priority updates;
- nghttp3 integration that uses its supported priority hooks rather than creating contradictory wire scheduling.

**Complete when:** all protocol frontends drive the shared stream contract, H2 large streams cannot monopolize renewed connection credit, H3 priority is coherently passed to nghttp3, and the normalized queue remains bounded under slow apps and peers.

#### Item 6 — SWAR/SIMD parsing and Wreath-owned QPACK blocking policy

**Commitment:** implement scalar, SWAR, and available SIMD delimiter/classification scanners for HTTP parsing, with runtime CPU dispatch. Incubate explicit instruction-set arms in metal, then make the selected safe native scanner available to the Wreath native server once parity is proven.

Required parsing coverage:

- request-line separators and invalid-byte classification;
- header colon/CR/LF discovery;
- resumable incremental scanning without rescans;
- bounded reads at page/allocation edges;
- scalar canonical fallback;
- architecture feature probing and differential malformed-input tests.

QPACK decoding remains performed by nghttp3, but Wreath must own the server policy and observability around blocking:

- configured dynamic-table and blocked-stream limits;
- per-connection blocked-stream accounting exposed through callbacks/API where available;
- memory retained by blocked request streams;
- known-received/required-insert-count progress telemetry where nghttp3 exposes it;
- timeout, reset, close, and excessive-load handling;
- proof that stalled encoder streams cannot retain unbounded Wreath application state.

This overrides the narrowing that treated the existing two nghttp3 settings as the entire item. Wreath does not duplicate the codec, but it does implement the lifecycle, limits, and pressure policy surrounding blocked streams.

**Complete when:** scalar/SWAR/SIMD arms are runtime-dispatched and differential-equivalent under forced test controls, incremental parsing remains linear, and QPACK blocking has explicit bounded Wreath-owned lifecycle/accounting in addition to nghttp3's codec state.

#### Item 7 — unified end-to-end credit coupling

**Commitment:** implement one explicit credit model connecting application readiness, normalized stream queues, protocol flow control, receive posting, send acceptance, and transport/socket drain across HTTP/1, HTTP/2, HTTP/3, and WebSocket.

Required inbound behavior:

- ASGI receive demand and queue budget determine whether metal posts/rearms receive;
- HTTP/1 pauses userspace receive and lets the TCP window close naturally;
- HTTP/2 returns `WINDOW_UPDATE` only from consumed application credit;
- HTTP/3 returns `MAX_STREAM_DATA`/connection credit only from consumed application credit;
- multishot receive cancellation/races return selected buffers correctly;
- no queue is allowed to absorb an unbounded producer/consumer mismatch.

Required outbound behavior:

- ASGI send completion consumes bounded stream/connection output credit;
- HTTP/2 protocol windows and transport high/low watermarks are coupled;
- HTTP/3 retained retransmission segments have byte and segment credits released by safe acknowledgement/ownership transfer;
- DRR schedules only streams holding all required credits;
- reset, disconnect, cancellation, and shutdown return or invalidate credit exactly once.

**Complete when:** slow applications, uploads, readers, and stalled peers have configured memory bounds at every protocol layer, and pressure propagates to the originating sender rather than accumulating in an internal queue.

### Gate override and retained non-waivable requirements

The architectural decision to implement all seven is final for this plan. The following former gates are reinterpreted:

- Phase measurements tune thresholds, budgets, and implementation details; they do not choose a different metal backend.
- “Below noise” is reported honestly but does not disable or delete committed metal ownership.
- “Deferred” means ordered after prerequisites, not out of scope.
- Asyncio/uvloop compatibility is a permanent supported lane, not a reason to omit metal.
- Linux privilege/kernel limitations require clear startup failure, not silent fallback or omission of the metal implementation.

The mandate does **not** waive:

- ASGI and protocol correctness;
- memory safety, sanitizer, fuzz, and stale-lifetime testing;
- bounded memory and overload behavior;
- TLS and CID security review;
- dependency-free `src/wreath` core and optional accelerated components;
- truthful benchmark methodology and prohibition on unmeasured performance claims;
- explicit backend selection and conforming fallback behavior.

### Immediate code-review findings

#### HTTP/3 response retention is not bounded by application backpressure

`src/wreath/_native/http3_asgi.c` appends each non-empty ASGI response body to a growing `resp_chunks` vector, asks nghttp3 to resume the stream, and immediately returns a resolved future. The exact Python `bytes` objects remain retained until acknowledgement accounting proves that nghttp3/ngtcp2 can no longer retransmit them.

A fast application serving a slow or stalled peer can therefore retain response chunks without a configured high-water limit. This is the clearest current credit-coupling gap. It can inflate RSS, allocator work, cache pressure, and the blast radius of one slow connection.

Wreath-wide action:

- Track retained response payload bytes and segment count per stream and connection.
- Introduce configurable high/low watermarks.
- Return a pending ASGI send waiter once the high watermark is crossed.
- Resolve waiters only as safe release, normally acknowledgement, takes retained ownership below the low watermark.
- If acknowledgement-based waiting is too conservative, use an explicitly bounded native retransmission arena; do not hide an unbounded copy behind a resolved ASGI send.
- Disconnect, reset, shutdown, and error paths must resolve or cancel every waiter exactly once.

#### HTTP/2 records transport pressure but does not enforce it

`Http2Protocol.write_paused` is set by `pause_writing()` and cleared by `resume_writing()`, but the value does not gate stream framing or ASGI send completion. `h2_flush_stream_pending()` considers HTTP/2 stream and connection windows, while `h2_flush()` transfers its bytearray to the transport regardless of transport pressure.

A peer can advertise a large HTTP/2 receive window while the TCP peer or TLS transport drains slowly. The application may then continue framing bytes into the transport after its high watermark has fired. HTTP/1 already has the stronger model: `maybe_drain()` returns a waiter while the transport is paused.

Wreath-wide action:

- Make transport writable credit a prerequisite for running the HTTP/2 write scheduler.
- Stop framing new DATA when `write_paused` is set, while preserving already-owned output.
- Resume bounded scheduler work from `resume_writing()`.
- Make ASGI send completion represent both protocol flow credit and accepted bounded transport ownership.
- Bound per-resume work so a large response cannot starve timers or unrelated streams.

#### HTTP/2 connection-window wakeup is unfair

On a connection-level `WINDOW_UPDATE`, `conn_blocked` is visited in list order. Each `h2_flush_stream_pending()` call loops until its body is complete or a window closes. An early large response can consume the renewed connection window before later streams run.

Wreath-wide action:

- Replace drain-until-blocked traversal with an active-stream scheduler.
- Use Deficit Round Robin for equal-urgency streams, charging DATA payload plus any deliberately selected framing cost.
- Persist deficits across rounds and cap frames/bytes per scheduler activation.
- Add RFC 9218 urgency bands only after basic fairness and transport-pressure coupling are correct.
- Preserve protocol-specific scheduling: HTTP/1 and WebSocket do not need multiplexed DRR, and HTTP/3 should use/configure nghttp3's scheduling API rather than compete with it from an independent generic queue.

#### The epoll callback table lacks stale-registration protection

Metal places an integer fd in `epoll_event.data.fd` and reloads an fd-indexed `FdEntry` during dispatch. If a callback closes a descriptor, another operation reuses the integer, and an older event for that fd remains later in the returned epoll batch, the stale event can reach the new registration.

Metal-only hardening action:

- Store a 64-bit `{generation, index}` registration token in `epoll_event.data.u64`.
- Validate the current slot generation before loading callback or connection state.
- Count stale events for diagnostics rather than treating them as an impossible case.
- Keep the implementation useful to the later completion ABI; do not build a separate 8-bit generation scheme for epoll.

### Wreath-wide integration track

The following work is not inherently dangerous or kernel-specific. It belongs in the shared Wreath server/protocol implementation, with pure/native parity where that contract exists and with asyncio/uvloop retained as first-class adapters.

#### End-to-end pressure and credit

Current strengths to preserve:

- HTTP/1 pauses reads on both queued-byte and queued-message watermarks and resumes only below low water.
- HTTP/1 exposes transport drain through an awaitable.
- HTTP/2 returns stream and connection `WINDOW_UPDATE` credit as ASGI consumes body chunks.
- HTTP/2 ASGI sends suspend when protocol flow-control windows are exhausted.
- HTTP/3 returns QUIC stream/connection credit as buffered request body is taken by ASGI.

Required completion:

- Couple HTTP/2 output to TCP/TLS transport pressure, not only HTTP/2 windows.
- Bound HTTP/3 retained response bytes and make ASGI send await that ownership budget.
- Keep byte and message/segment limits; a queue of empty chunks can defeat a byte-only limit.
- Make cancellation and disconnect release credits and ownership without double resolution.
- Expose pressure counters through existing diagnostics rather than introducing hidden global state.

On TCP, stopping userspace receive does not prevent the kernel receive buffer from filling; it eventually shrinks the advertised receive window. Documentation must describe that accurately rather than claiming that no byte is buffered below ASGI. On a future multishot receive path, exhausting application credit must stop, cancel, or avoid rearming receive operations while still returning buffers from racing completions.

#### HTTP/2 ingress storage

HTTP/1 already supports direct buffered receive into its C-owned parser allocation. HTTP/2 currently receives a Python `bytes`, copies it into a growing C buffer, and compacts a consumed prefix after parsing.

Wreath-wide action:

- Add a correct `BufferedProtocol`-style or transport-neutral direct ingress adapter for HTTP/2.
- Prefer a segmented parser or cursor-based storage that avoids whole-tail compaction.
- Permit tiny bounded copies for boundary structures such as the 9-byte frame header instead of requiring every payload to be linearized.
- Preserve frame-size, continuation, HPACK, malformed-input, and flow-control behavior exactly.

This is a lower-risk path to fewer copies than introducing a global provided-buffer arena directly into the current protocol object.

#### Shared stream semantics without a new dispatch queue

HTTP/1, HTTP/2, HTTP/3, and WebSocket currently maintain separate native state and ASGI bridges. ASGI already normalizes much of the application-facing interface, but it intentionally exposes `scope["http_version"]`, and WebSocket has distinct scopes/messages. The wire version must not be erased.

Wreath-wide action:

- Share literal helper APIs for handler activation, body-credit return, response-segment ownership, disconnect, reset, trailers, and completion.
- Use a narrow internal driver/vtable only where it removes duplicate ownership logic.
- Keep protocol-specific structs and wire state.
- Do not insert a mandatory normalized-event dispatch queue between parser and handler; it would add an allocation/queue boundary to a path that currently activates handlers directly.

#### DRR and RFC 9218

Introduce scheduling in stages:

1. Transport-aware HTTP/2 active-stream queue.
2. Equal-service DRR with persistent deficit and bounded scheduler activations.
3. Urgency bands.
4. RFC 9218 `incremental` behavior within a band.
5. Priority-header/PRIORITY_UPDATE parsing and abuse limits.

Non-incremental does not mean an unlimited drain that can block higher urgency work. Higher urgency, control frames, shutdown, timers, and transport pressure remain preemptive constraints. Report fairness and tail behavior separately from throughput when this work is eventually measured.

#### Parsing and compression

HTTP/1 already avoids quadratic incremental rescans by retaining per-state scan cursors. The remaining delimiter and token/value scans are scalar C loops.

Wreath-wide safe progression:

- First use simple bounded `memchr()` candidate discovery where it preserves validation; libc commonly supplies architecture-tuned implementations.
- Keep the scalar parser canonical and behaviorally identical.
- Avoid hand-written SWAR/SIMD until parser work is isolated by an ablation and shown material to the whole request.
- Keep CPython object construction and header allocation in the cost model; delimiter scanning alone may not dominate.

HPACK's Huffman decoder already uses a byte-transition table. QPACK blocked-stream bookkeeping must remain owned by nghttp3. Wreath already configures `qpack_max_dtable_capacity` and `qpack_blocked_streams`; do not build a second QPACK blocker beside the library. Add Wreath-side limits, telemetry, and error mapping only where nghttp3's API requires them.

#### Transport-neutral protocol driver

Before another backend is added, separate protocol behavior from Python transport calls:

- ingress bytes/buffer segments;
- writable notification and output-segment ownership;
- timer expiry;
- close/reset/disconnect;
- application activation and suspension;
- pressure credit return.

The asyncio/uvloop adapter remains the reference production adapter. Metal consumes the same driver directly. This prevents io_uring work from being embedded in `SocketTransport` callbacks and prevents protocol semantics from forking by loop.

#### Ordinary multiworker support

Safe worker scaling can be Wreath-wide:

- explicit worker processes on regular GIL CPython;
- one loop and listener ownership domain per worker;
- `SO_REUSEPORT` as an explicitly configured distribution option;
- no mutable cross-worker connection table;
- clear socket inheritance, graceful restart, and shutdown rules.

Established TCP connections are already attached to the socket that accepted them; no BPF is required to keep TCP keep-alives pinned. Thread-per-core Python workers are a separate free-threading implementation and must not be assumed correct or parallel on a normal GIL build.

### `wreath-metal` isolation track

The following work is the Linux-specific `wreath-metal` architecture. It fails clearly when required kernel facilities are unavailable and remains isolated from the separate asyncio/uvloop-backed `wreath-native` tier.

#### Completion ABI and io_uring

Metal exposes one io_uring-owned completion record and operation API. Handlers and protocol drivers submit operations; the native loop performs syscalls, drains a bounded completion batch, validates ownership, drives state machines, gathers resulting submissions, and submits once per bounded iteration. Generic descriptor readiness is part of that tagged CQ domain; metal has no epoll aggregation fd or event array.

Initial io_uring progression:

1. ordinary accept/recv/send;
2. batched submission and completion drain;
3. multishot accept;
4. multishot receive with selected provided buffers;
5. cancellation and stale-generation handling;
6. fixed/direct descriptors only after an isolated comparison;
7. zero-copy send only with complete notification-lifetime accounting and a payload threshold.

Explicit io_uring selection must fail rather than silently use epoll. Kernel feature, seccomp, sysctl, and container restrictions are normal capability outcomes.

#### Adaptive spin-then-block

The current metal loop performs a nonblocking `epoll_wait(..., 0)` and, when empty, may perform a second blocking `epoll_wait()`. A future CQ path may optionally spin on the userspace CQ tail before entering a blocking `io_uring_enter(GETEVENTS)`.

Safety and control rules:

- Never spin while Python callbacks are ready, timers are due, control messages are pending, or shutdown needs progress.
- Track empty-CQ-to-next-arrival gaps, not every intra-burst CQE interval; otherwise a burst's short internal spacing teaches the loop to burn CPU through a long inter-burst idle.
- Maintain EWMA mean plus absolute deviation/variance and use variance as a confidence gate.
- Spin only when the predicted gap is below the measured sleep/wakeup break-even point and variability is low.
- Clamp by a strict time budget, CPU-efficiency policy, and empty-epoch hysteresis.
- Release the GIL while polling a loop-owned ring on regular CPython.
- Keep blocking as the low-load default and expose spin time, hit rate, misses, and avoided enters.
- Do not enable `SQPOLL` by default; it dedicates kernel CPU and is a separate implementation.

Adaptive spinning may lower wake latency at sustained load, but it can regress CPU/request, colocated workloads, thermal behavior, and tail latency under bursty traffic. It is not a default merely because it reduces enter calls.

#### Linked SQE implementations

Do not treat `accept -> recv` as a generally available linked graph:

- an ordinary linked receive cannot consume the fd returned by a preceding accept;
- a chosen direct/fixed-file slot is required for such a handoff;
- short socket reads are normal and interact with link failure/cancellation semantics;
- connection allocation, limits, TLS, and protocol setup are data/state dependent;
- HTTP and QUIC handshakes are not fixed kernel-only graphs.

Prefer multishot accept/receive and one batched resubmission after state-machine dispatch. Permit linked or hard-linked chains only as narrow metal implementations with explicit direct-slot ownership and differential error/cancellation tests.

#### Generational arenas

Metal connections, streams, operations, and buffers may move into per-worker contiguous arenas after the transport-neutral driver exists.

Use 64-bit tokens, not `{index:24, generation:8}`. Eight generation bits wrap after only 256 slot reuses and permit ABA under realistic churn. A token normally identifies an operation slot; that operation contains a separately validated connection/stream handle. Multishot operations remain live until the CQE clears `MORE`.

On completion:

1. validate operation generation;
2. release backend-owned buffer/notification resources even if stale;
3. validate referenced connection/stream generation;
4. dispatch only to a live incarnation;
5. record stale/cancel-race counters.

“Drop stale completion” means drop state-machine delivery, not leak its provided buffer or zero-copy notification. Branchless dispatch is not a design goal; correct, observable ownership is.

#### Provided-buffer rings, arenas, and NUMA

Provided buffers are allocated/provided before receive; they do not magically avoid memory commitment or guarantee an entire TCP protocol unit is contiguous. A selected receive buffer is contiguous, but one HTTP record may span several receives.

Metal-only progression:

- fixed-size per-worker receive pools with explicit buffer IDs and bounded exhaustion policy;
- the same ownership API for epoll and io_uring;
- selected provided buffers on supported io_uring kernels;
- segmented protocol input and explicit retain/release;
- first-touch/NUMA placement only after the worker is pinned;
- counters for pool occupancy, exhaustion, retained buffers, and cross-node allocation.

A bip buffer may be tested for a per-connection TCP ingress stream where it reduces wrap/compaction. It does not by itself linearize a record split across independent recv reservations, and it does not automatically compose with a shared provided-buffer ring. QUIC datagrams naturally fit one sufficiently sized selected buffer; TCP parsers should accept segments or make only tiny bounded boundary copies.

#### Per-core QUIC CID steering and eBPF

Only after explicit worker/reuseport support exists, metal may add QUIC-LB-style server connection IDs and a reuseport eBPF selector:

- encode an authenticated or cryptographically blinded worker route in every Retry and subsequent server-issued CID;
- keep a fixed CID format/length that a bounded BPF parser can safely locate in long and short headers;
- route client-selected Initial CIDs by the normal initial distribution policy;
- steer migration/rebinding packets carrying server CIDs to the owning worker;
- avoid shared connection lookup or cross-worker state migration;
- define key rotation, worker-count changes, graceful restart, and invalid-CID behavior;
- replace the current development `rand()` and static secret before production use.

This is a multicore scalability and locality feature, not a single-worker latency optimization. It requires privileges and platform support, so a userspace fallback and an explicit capability report are mandatory. A simpler TCP reuseport BPF program is unnecessary for established keep-alives and should exist only if initial connection distribution demonstrates a separate need.

#### Explicit SIMD and interpreter-specific fast paths

Architecture-specific HTTP scans, generated parsers, direct CPython task internals, and other interpreter-specific tricks should incubate in metal or an optional native feature arm. They require:

- runtime CPU feature probing;
- a scalar canonical implementation;
- exact malformed-input differential tests;
- sanitizer/fuzzer coverage;
- bounded reads at allocation and page edges;
- independent ablation before promotion to the shared native server.

Do not move HPACK/QPACK ownership away from the existing table decoder/nghttp3 merely to make a SIMD claim.

### Reconciled delivery order

1. **Wreath-wide:** bound HTTP/3 response retention and connect ASGI sends to retained-byte credit.
2. **Wreath-wide:** make HTTP/2 honor transport pressure.
3. **Wreath-wide:** introduce bounded HTTP/2 DRR, then RFC 9218 urgency/incremental behavior.
4. **Metal:** add 64-bit generations to the existing epoll registration table and bounded per-iteration dispatch budgets.
5. **Wreath-wide:** extract the transport-neutral protocol driver while retaining asyncio/uvloop as the reference adapter.
6. **Wreath-wide:** remove avoidable HTTP/2 ingress copying through direct or segmented storage.
7. **Wreath-wide:** add explicit process-worker/reuseport ownership and lifecycle.
8. **Metal:** introduce operation, connection, stream, and buffer slot tables with 64-bit generations.
9. **Metal:** implement ordinary io_uring behind the completion ABI, then independently add multishot and provided-buffer features.
10. **Metal:** evaluate adaptive CQ spinning; keep linked SQEs, fixed files, and zero-copy send as separate implementations.
11. **Metal:** add QUIC-LB CID/eBPF steering only after the worker model and secure CID format exist.
12. **Wreath-wide or promoted from metal:** consider `memchr`, SIMD, or SWAR parser changes only after whole-request evidence shows parsing remains material.

### Expected performance effects and uncertainty

Higher-confidence code-derived effects:

- bounded HTTP/3 and HTTP/2 output ownership should control RSS and protect latency under slow peers;
- HTTP/2 DRR should reduce stream starvation and improve multiplexed tail fairness;
- generation tokens should eliminate stale-registration/completion ABA classes;
- direct/segmented HTTP/2 ingress should remove copies and compaction work visible in the current path;
- a transport-neutral driver should remove Python adapter boundaries on metal without forking protocol semantics.

Effects that require especially strong evidence:

- adaptive spinning can exchange CPU efficiency for wake latency;
- io_uring can lose to direct epoll for small operations or restricted kernels;
- per-core slabs can increase reserved memory and complicate Python ownership;
- DRR may emit more/smaller frames and reduce bulk throughput if budgets are poorly chosen;
- provided rings do not remove all protocol-boundary copies;
- hand-written SIMD can lose to libc/compiler code or be dominated by Python header allocation;
- eBPF CID steering improves scale/locality but adds operational and security complexity;
- linked SQE graphs may provide no usable protocol win after direct-slot and error semantics are included.

No item may be described as a performance win from this review alone. Future work must follow the repository's ablation, repeated-trial, A/A noise-floor, tail-latency, CPU/request, memory, and correctness requirements.

## Research conclusions that constrain the design

### libuv and uvloop

libuv is a mature, general cross-platform C runtime, not merely an epoll wrapper. It provides handles, streams, timers, callback phases, DNS/filesystem work, signals, processes, a worker pool, and platform backends for epoll, kqueue, and IOCP. uvloop adds an optimized Cython asyncio implementation over it.

A Wreath loop can remove generality and object/callback layers that Wreath does not need on its server fast path. It cannot assume direct epoll or io_uring alone beats libuv.

### io_uring

Relevant Linux facilities include batched submission/completion rings, multishot accept, multishot receive with provided buffers, fixed/direct descriptors, cancellation, and zero-copy sends. Multishot receive requires Linux 6.0 or newer. Zero-copy send usually has a second completion that controls buffer reuse.

These features are optional and runtime-probed. Kernel version, sysctl, seccomp/container policy, or security posture may disable io_uring. A direct epoll backend is required both as a baseline and fallback. `SQPOLL` is not an initial feature: eliminating syscalls by burning a polling core is not automatically an efficiency gain.

### Modern runtime lessons

libxev and TigerBeetle demonstrate a useful common abstraction: completion-oriented operations over io_uring while adapting epoll/kqueue underneath. Glommio, Monoio, and Seastar emphasize thread-per-core ownership, local queues, CPU affinity, batching, and avoiding cross-core locks.

Wreath should borrow local ownership and completion records, not import these runtimes or quote their benchmarks as Wreath evidence.

### CPython 3.14 integration

The supported installation seam is `asyncio.run(..., loop_factory=...)` / `asyncio.Runner(loop_factory=...)`; the policy system is deprecated and scheduled for removal in Python 3.16.

A drop-in loop is much broader than socket polling: callbacks, timers, tasks/futures, thread-safe wakeups, transports/protocols, DNS/executors, SSL, signals, subprocesses, cancellation, exceptions, debug behavior, and shutdown all matter. The plan proves a Wreath server fast lane before attempting complete asyncio compatibility.

## Architectural direction

Add a separately importable optional extension, provisionally `wreath._native._loop`. Keep `_core`, `_server`, `_client`, `_postgres`, `_http3`, and `_loop` independently importable as required by the existing extension boundaries.

The loop exposes a completion-oriented internal ABI:

```c
typedef enum {
    WREATH_OP_ACCEPT,
    WREATH_OP_RECV,
    WREATH_OP_SEND,
    WREATH_OP_TIMEOUT,
    WREATH_OP_WAKE,
    WREATH_OP_CANCEL
} wreath_op_kind;

typedef struct {
    uint64_t token;       /* slot + generation, never a raw pointer */
    int32_t result;       /* bytes/fd or negative platform error */
    uint16_t kind;
    uint16_t flags;       /* MORE, BUFFER, EOF, NOTIFICATION, ... */
    uint32_t value;       /* buffer id or backend-specific compact value */
} wreath_completion;
```

Internal operations:

```text
submit_accept(listener, token)
submit_recv(connection, buffer_group, token)
submit_send(connection, immutable_segments, token)
submit_timeout(deadline, token)
submit_cancel(token)
poll(completions, capacity, deadline)
wake()
```

Backends normalize ownership, completion, cancellation, and stale-operation rejection. They do not pretend epoll readiness, io_uring completion, kqueue filters, and IOCP are identical internally.

### Fast lane and compatibility lane

1. **Wreath server fast lane:** listener and accepted sockets are loop-owned; completions drive the existing native HTTP protocol state directly; immutable response bytes go directly to the backend. No asyncio transport object or Python `transport.write()` call is involved.
2. **Asyncio compatibility lane:** Python callbacks, tasks, futures, timers, `add_reader`/`add_writer`, thread-safe wakeups, and eventually standard transports are provided for handlers and third-party async libraries.
3. **Fallback lane:** unsupported protocols/features continue to use the current asyncio/uvloop server without silent semantic downgrade.

The server selects a loop only when explicitly configured. Wreath never changes an application's global loop implicitly.

## Native ownership model

### `WreathLoop`

One loop belongs to one OS thread and initially one server worker. It owns:

- backend state and feature bits;
- bounded completion batch storage;
- ready callback queue;
- timer structure;
- cross-thread wake mailbox;
- listener and connection slot tables;
- receive-buffer pools;
- direct protocol-driver references;
- loss/pressure/debug counters;
- explicit lifecycle state.

No connection migrates between loop threads. A future worker-per-core model uses socket distribution or `SO_REUSEPORT`, not shared mutable connection tables.

### Connection and operation tokens

Use fixed/growing-at-startup slot tables and 64-bit `{generation, index}` tokens. Every completion validates the generation before touching connection/request state. Cancellation, close, descriptor reuse, delayed zero-copy notification, and HTTP/2/3 stream reuse must not turn a stale completion into a use-after-free.

### Receive buffers

The loop owns receive buffers until protocol consumption returns them. For io_uring, buffer IDs map directly to provided-buffer-ring entries. For epoll, the same IDs map to the loop pool. Protocol code receives `(buffer_id, pointer, length)` and explicitly releases or retains it.

Do not promise zero-copy request bodies through ASGI initially. The first goal is to avoid allocator and transport copies inside the native ingress path while preserving current request-body semantics.

### Send ownership

Existing immutable Python bytes and native response segments remain valid until send completion. Ordinary sends complete once the backend no longer references them. `SEND_ZC` adds a notification state: memory is reusable only after the notification completion. Enable it only above a measured payload threshold.

### Ready queue and timers

Start with a simple FIFO ready queue and indexed min-heap timers. Do not add a timing wheel until timer population/deadline benchmarks show the heap is material. Maintain bounded native request-path entries; generic Python `call_soon` may allocate Python handles because compatibility behavior requires Python object ownership.

### Cross-thread wakeup

Use one bounded MPSC mailbox plus `eventfd` on Linux. Producers enqueue before signalling; the loop drains messages in batches. Queue saturation fails the submitted control operation explicitly or falls back to the documented thread-safe slow path; it never corrupts ordering.

## Implementation and evidence phases

Each phase is an implementation and evidence checkpoint. All phases required by the seven-item mandate will be implemented in dependency order. A future agent must retain raw artifacts, environment metadata, A/A runs, and correctness results; those results govern defaults, promotion, configuration, and claims rather than cancelling committed implementation work.

### Phase 0 — price the existing boundary

**Scope**

Measure how much current request cost is attributable to the asyncio/uvloop transport and scheduling seam before writing a replacement.

Add controlled ablations for:

- transport callback ingress only;
- native parser/protocol drive without application work;
- `transport.write()`/`writelines()` response emission;
- loop task scheduling versus eagerly completed handlers;
- timers enabled/disabled;
- asyncio versus uvloop on identical Wreath native-server workloads.

**Repository surfaces**

- `src/wreath/_devtools/measure.py` and a focused decomposition command;
- `benchmarks/wreath_server.py` and benchmark metadata;
- no production behavior change.

**Proof required**

- Interleaved repeated A/A and A/B trials on an idle machine.
- Empty native response, empty Python handler, small JSON, streaming, and high concurrency.
- Cycles, instructions, cache misses, syscalls, context switches, allocations, throughput, p50/p99/p999, and Python/native crossings.
- Ablations must still perform equivalent parsing, response bytes, connection reuse, and error handling.

**Evidence interpretation**

Price the transport/scheduler seam before selecting defaults or claiming value. If the removable seam is below the measured noise floor, publish that result and keep the affected implementation metal-specific or disabled by default; do not cancel the seven-item implementation mandate.

**Deferred**

No new loop, io_uring, portability, or public configuration.

### Phase 1 — transport-neutral protocol driver

**Scope**

Separate the existing server protocol state from Python transport calls without changing HTTP behavior. Add a private driver interface for ingress bytes, writable notification, timer expiry, close, and immutable output segments. Keep the asyncio transport adapter as the reference production adapter.

**Repository surfaces**

- `server.h`, `server_http1.c`, `server_http2.c`, HTTP/3 seams where applicable;
- new private `server_driver.h` / `server_driver.c`;
- existing fake-transport protocol tests.

**Proof required**

- Byte-for-byte response parity through old and new adapters.
- Exact parity for malformed input, pipelining, body framing, backpressure, cancellation, disconnect, timeout, WebSocket, and shutdown.
- No benchmark claim yet; prove that the adapter split does not regress the current asyncio/uvloop path outside noise.

**Completion criterion**

All current protocol suites pass through both adapters, native lints and sanitizers are clean, and the current adapter remains the default.

**Deferred**

Direct sockets, Python event-loop API, TLS ownership, io_uring.

### Phase 2 — Linux direct-epoll vertical slice

**Scope**

Implement a Linux-only C reactor for plaintext HTTP/1.1 using:

- `epoll_create1` and batched `epoll_wait`/`epoll_pwait2` where available;
- edge-triggered readiness;
- `accept4(SOCK_NONBLOCK | SOCK_CLOEXEC)`;
- recv/send draining to `EAGAIN`;
- `eventfd` wakeup;
- fixed connection slots and receive buffers;
- direct protocol-driver ingress/output.

This epoll-only phase is historical scaffolding. It is not a product backend and must not remain under `wreath-metal` once unified io_uring waiting is complete.

**Repository surfaces**

- new `_native/_loopmodule.c`, `loop.h`, `loop_common.c`, `loop_epoll.c`, buffer/slot helpers;
- `setup.py` with platform guards;
- explicit selection in `server.py`, `_cli.py`, and `benchmarks/wreath_server.py`;
- focused native-loop tests.

**Proof required**

- Plain HTTP/1 static/native response first, then Wreath application activation.
- Equivalent response bodies, connection behavior, limits, timeouts, errors, and shutdown.
- Independent load generator as well as the bundled development generator.
- Compare asyncio, uvloop, and direct epoll on the same app, CPU affinity, loop policy, protocol, concurrency, and duration.
- Saturate accept, reads, writes, idle keep-alive, slow readers/writers, and connection limits.

**Gain criterion**

Do not call it faster unless repeated result ranges separate according to the benchmark suite's winner rule and the gain clears A/A noise. Require either:

- higher throughput with no p99/p999, error, CPU, or memory regression; or
- lower cycles/CPU per equivalent request at equal throughput and latency.

A fully native fixed-response win is only a transport proof. Promotion requires the win to survive a Python handler and small-JSON workload.

**Deferred**

TLS, HTTP/2, HTTP/3, complete asyncio API, macOS/Windows.

### Phase 3 — Python scheduler compatibility required by Wreath handlers

**Scope**

Implement the minimum correct CPython 3.14 event-loop surface needed by Wreath and its owned clients/database paths:

- `run_forever`, `run_until_complete`, stop/close;
- `call_soon`, `call_later`, `call_at`, cancellation;
- `call_soon_threadsafe` and wake mailbox;
- `create_future`, `create_task`, task factory/context behavior;
- loop exception handler and monotonic `time()`;
- executor/DNS delegation through explicit bounded off-loop facilities;
- server lifecycle, signals, and async-generator/default-executor shutdown used by `asyncio.run`/`Runner`.

Integrate through an explicit `loop_factory`. Reuse CPython's Task/Future behavior where possible rather than creating a second coroutine scheduler prematurely.

**Proof required**

- Focused CPython asyncio behavioral tests for implemented methods.
- Wreath HTTP client, PostgreSQL, ORM, lifespan, background task, cancellation, timeout, and shutdown suites under the loop.
- Third-party compatibility smoke tests only for dependencies already in declared test/benchmark groups.
- Exact context-variable propagation, cancellation ordering, and exception reporting.
- `wreath-request-trace` records and explains any changed boundary crossings.

**Gain criterion**

The end-to-end Wreath applications that actually suspend—database call, outbound HTTP, streaming, background handoff—must preserve the Phase 2 advantage or the fast lane remains a specialized native-response backend rather than a general loop.

**Deferred**

Full drop-in asyncio coverage, subprocesses/pipes, TLS, io_uring.

### Phase 4 — io_uring backend

**Scope**

Implement the same completion ABI over io_uring with runtime feature probing:

1. ordinary accept/recv/send submissions;
2. batched SQ submission and CQ draining;
3. multishot accept;
4. multishot recv with provided buffers on supported kernels;
5. cancellation and stale-generation handling;
6. fixed/direct descriptors only after an ablation;
7. `SEND_ZC` only after payload-threshold and notification-lifetime tests.

Do not enable `SQPOLL` by default. Treat unavailable or denied io_uring as a normal reason to select epoll, while explicit `wreath-uring` mode fails clearly rather than silently changing backend.

**Proof required**

- Feature-probe tests across supported kernel capability combinations.
- Differential event traces against epoll for accept/read/write/EOF/error/cancel/timeout.
- Buffer exhaustion and return, CQ overflow, multishot termination/rearm, descriptor reuse, stale CQEs, and zero-copy notification tests.
- ASan/UBSan plus fault injection at every submission/completion transition.
- Bench io_uring against direct epoll, not only against uvloop.
- Ablate each feature independently; report ordinary io_uring, multishot, provided buffers, fixed files, and zero-copy separately.

**Gain criterion**

io_uring is the metal ownership architecture. Measurements compare and improve its p99/p999, CPU efficiency, memory, and failure behavior; they do not replace it with an asyncio or epoll product path.

**Deferred**

`SQPOLL`, cross-worker shared rings, file I/O generalization.

### Phase 5 — fairness, timers, and operational hardening

**Scope**

Prevent benchmark-friendly starvation and make the loop operationally bounded:

- per-iteration completion, callback, accept, and write budgets;
- fair requeueing of hot connections;
- timer accuracy under sustained I/O;
- bounded connection, completion, mailbox, and buffer pools;
- overload policy and pressure counters;
- graceful drain and forced-close deadlines;
- fork/restart and file-descriptor inheritance rules;
- Flight Recorder integration using the same native lifecycle IDs when that subsystem exists.

**Proof required**

- Hot connection versus many cold connections.
- Slowloris, slow reader, slow producer, callback storm, timer storm, and mailbox saturation.
- p999 and maximum event-loop lag, not throughput alone.
- Long soak with connection churn, cancellation, and exporter/diagnostic pressure.
- Fixed memory high-water derived from configured budgets.

**Completion criterion**

No workload can starve timers, shutdown, control messages, or unrelated connections beyond documented budgets. Overflow is explicit and non-corrupting.

**Deferred**

TLS and additional platforms.

### Phase 6 — HTTP/2, WebSocket, UDP, and optional HTTP/3 fast lanes

**Scope**

Move existing protocol drivers onto direct loop ownership one at a time:

1. WebSocket over HTTP/1;
2. HTTP/2 streams and connection flow control;
3. UDP endpoint support required by optional HTTP/3;
4. HTTP/3 only with its existing optional ngtcp2/nghttp3 boundary.

Preserve the current protocol compliance suites and separate protocol benchmarks. Do not conflate a protocol change with an event-loop gain.

**Proof required**

- Existing HTTP/2/3 fake-transport and compliance suites adapted to the transport-neutral driver.
- Independent `h2load`-class generation, flow-control pressure, reset, GOAWAY, QUIC timer, and connection migration/non-migration behavior as applicable.
- Per-protocol asyncio/uvloop/direct-loop comparisons with identical TLS and application work.

**Completion criterion**

Each protocol is enabled only after semantic parity and retained protocol-specific evidence. Unsupported combinations fail clearly.

**Deferred**

TLS fast path unless approved separately.

### Phase 7 — TLS design gate

**Scope**

Measure and choose a TLS ownership model. Current Wreath relies on asyncio/uvloop transports and Python `SSLContext`; direct OpenSSL calls would alter build dependencies, security ownership, certificate behavior, and maintenance obligations.

Evaluate separately:

- keeping TLS on the current asyncio/uvloop path;
- a Python `SSLObject`/MemoryBIO compatibility path, measured for crossings and copies;
- an optional native TLS provider extension with an explicit dependency and security/update policy.

**Proof required**

- TLS handshake, resumption, ALPN, certificate validation, close-notify, truncation, backpressure, and error parity.
- Plaintext and TLS measurements reported separately.
- Security review and dependency/build decision before native provider integration.

**Completion criterion**

No blanket “faster server” claim includes TLS until the selected path clears the same correctness and performance gates. A plaintext-only loop remains valid if documented narrowly.

**Deferred**

No in-tree TLS implementation and no use of CPython/OpenSSL private ABI without an accepted ADR.

### Phase 8 — portability and public support decision

**Scope**

Only after Linux demonstrates durable gains:

- add kqueue backend for macOS/BSD;
- add IOCP backend for Windows;
- decide whether full asyncio compatibility is worthwhile or the loop remains Wreath-server-specific;
- define fallback, packaging, kernel/OS support, and compatibility policy;
- decide whether any backend can become default.

**Proof required**

- Backend differential traces and platform CI.
- Platform-native cancellation, wakeup, timer, socket, and subprocess semantics where claimed.
- No emulation layer may silently weaken behavior.
- Published support matrix and benchmark artifacts for each platform.

**Default-promotion criterion**

The loop becomes a default only after production-hardening evidence, compatibility documentation, an accepted ADR, and repeated representative wins over uvloop. Linux-only gains do not justify changing defaults on other platforms.

## Benchmark matrix

### Workloads

- native fixed response;
- empty Python handler;
- small and large JSON;
- parameter and protected routes;
- request/response streaming;
- WebSocket echo, idle population, fragmentation, ping/close;
- HTTP/1 keep-alive and pipelining;
- HTTP/2 concurrent streams and flow control;
- HTTP/3 only when built;
- PostgreSQL acquire/read/write/transaction and ORM hydration;
- outbound HTTP DNS/connect/TLS/reuse/retry;
- background work and genuine coroutine suspension;
- many idle connections, connection churn, slow peers, and overload.

### Compared arms

- Wreath native server on stock asyncio;
- Wreath native server on uvloop;
- Wreath direct epoll;
- Wreath io_uring base;
- io_uring feature ablations;
- telemetry-free/Off modes where Flight Recorder is present.

Framework comparisons behind Uvicorn remain separate. This plan measures Wreath's server, not framework overhead against competitors.

### Required measurements

Record Python version/mode, Wreath/native build hashes, compiler/flags, kernel and io_uring feature probe, CPU model/governor/affinity, event loop/backend, protocol, TLS, concurrency, duration, warmup, load generator/version, and limits.

Measure:

- throughput and completed work integrity;
- p50, p95, p99, p999, and maximum loop lag;
- errors, disconnects, resets, and rejected overload;
- CPU/request, cycles, instructions, branches, cache misses;
- syscalls, context switches, wakeups, SQ submissions, CQ completions;
- allocations, RSS, buffer/slot occupancy and drops;
- Python/native crossings and lifecycle phase attribution.

Use repeated interleaved trials and retain raw results. Establish A/A noise on the same day and configuration. A single run, microbenchmark-only result, or syscall-count reduction is not a gain claim.

## Correctness rules

- Preserve ASGI semantics and Wreath protocol behavior before optimizing.
- Keep asyncio and uvloop available throughout development.
- Never select a backend silently when an explicit backend was requested.
- Never dereference a completion without validating slot generation.
- Buffer reuse follows backend completion ownership, including zero-copy notification CQEs.
- Event-loop failure, queue saturation, or unsupported kernel features produce bounded actionable errors—not hangs or memory growth.
- No global mutable connection state; ownership is loop/worker explicit.
- Free-threading correctness relies on ownership and atomics, not the GIL.
- Do not use cProfile to choose optimizations. Ablate the whole request and use existing measurement tooling.
- Keep `wreath-native-lint`, sanitizers, fuzzers, request-boundary checks, Ruff, ty, pytest, and strict docs gates clean as applicable.

## Expected files

### Add

```text
src/wreath/_native/loop.h
src/wreath/_native/loop_common.c
src/wreath/_native/loop_epoll.c
src/wreath/_native/loop_uring.c
src/wreath/_native/loop_buffers.c
src/wreath/_native/loop_timers.c
src/wreath/_native/_loopmodule.c
src/wreath/_native/server_driver.h
src/wreath/_native/server_driver.c

tests/native_loop/
benchmarks/bench_native_loop.py
```

Later, only after approved gates:

```text
src/wreath/_native/loop_kqueue.c
src/wreath/_native/loop_iocp.c
```

### Change

```text
setup.py
pyproject.toml
src/wreath/server.py
src/wreath/_cli.py
src/wreath/_native/server.h
src/wreath/_native/server_common.c
src/wreath/_native/server_http1.c
src/wreath/_native/server_http2.c
src/wreath/_native/http3.h
src/wreath/_native/http3_asgi.c
benchmarks/wreath_server.py
benchmarks/run.py
benchmarks/README.md
repo-map.md
docs/agents/manifest.json
```

Do not add libuv, liburing, libxev, or another runtime as a mandatory dependency. A direct syscall implementation keeps the default native build dependency-free beyond compiler and CPython headers. If using liburing is later proposed for maintainability, isolate it behind the io_uring backend and justify the dependency against the small syscall wrapper actually required.

## Risks, containment, and promotion stop conditions

- The removable libuv/asyncio cost may be below noise after existing Wreath native optimizations. That result blocks default promotion and performance claims, but does not cancel implementation of the accepted seven-item metal arm.
- A fixed-response win may disappear when Python handlers, database calls, TLS, or streaming dominate.
- io_uring can lose to direct epoll for small network operations or restricted environments.
- Full asyncio compatibility can exceed the value of the fast path. Keep the Wreath-specific lane independently useful.
- TLS may erase or reverse plaintext gains and materially expands security/build responsibility.
- Completion cancellation and buffer lifetime bugs are memory-safety defects; simplify or stop rather than waive evidence.
- Thread-per-core scaling may conflict with Python object ownership and free-threaded behavior; it is a separate measured step.
- A custom loop becomes long-lived platform code. Do not promote it if gains are marginal, fragile, or confined to synthetic tests.

## Explicit non-goals

- Replacing asyncio for applications that do not choose Wreath's server.
- Claiming io_uring is inherently faster than epoll/libuv.
- Building a general filesystem/process runtime before server networking proves value.
- Adding a mandatory TLS, liburing, libxev, DPDK, eBPF, or kernel-bypass dependency.
- Kernel bypass, default `SQPOLL`, or dedicated polling cores in the initial implementation. Bounded adaptive userspace CQ spinning is explicitly committed to the metal tier and is not covered by this non-goal.
- Redesigning routing, ORM, middleware, telemetry, or public ASGI APIs.
- Changing Wreath's default loop before compatibility, production-hardening, and repeated end-to-end evidence.

## Research references

- [libuv design overview](https://docs.libuv.org/en/v1.x/design.html)
- [uvloop README](https://raw.githubusercontent.com/MagicStack/uvloop/master/README.rst)
- [CPython 3.14 asyncio runners and `loop_factory`](https://docs.python.org/3.14/library/asyncio-runner.html)
- [CPython 3.14 event-loop API](https://docs.python.org/3.14/library/asyncio-eventloop.html)
- [PEP 3156 event-loop contract](https://peps.python.org/pep-3156)
- [io_uring manual](https://man7.org/linux/man-pages/man7/io_uring.7.html)
- [io_uring multishot receive](https://man7.org/linux/man-pages/man3/io_uring_prep_recv_multishot.3.html)
- [io_uring multishot accept](https://man7.org/linux/man-pages/man3/io_uring_prep_multishot_accept.3.html)
- [io_uring zero-copy send](https://man7.org/linux/man-pages/man3/io_uring_prep_send_zc.3.html)
- [libxev completion-oriented cross-platform design](https://raw.githubusercontent.com/mitchellh/libxev/main/README.md)
- [TigerBeetle abstraction over io_uring and kqueue](https://tigerbeetle.com/blog/2022-11-23-a-friendly-abstraction-over-iouring-and-kqueue/)
- [Glommio thread-per-core io_uring design](https://docs.rs/glommio/latest/glommio/)
- [Seastar shared-nothing design](https://seastar.io/shared-nothing/)

## Metal structural baselines

The published champion checkpoint is [in the server guide](../../guides/server.md#metal-champion-checkpoint).
Keep both documents synchronized when a guarded artifact changes.

Keep this table current whenever ring ownership, arena sizing, event capacity, or
diagnostic allocation changes. These counters describe memory owned directly by
the native metal poller; they are deterministic regression targets and a leading
indicator for RSS, not a substitute for process-RSS measurements.

Run:

```console
uv run python -c 'import sys, wreath.reactor as r; l=r.metal_event_loop(); print(sys.version.split()[0], l._poller.native_ring_count, l._poller.native_mapped_bytes, l._poller.native_heap_bytes); l.close()'
```

| Checkpoint | Python | Rings | Native mmap bytes | Native heap bytes | Change |
| --- | ---: | ---: | ---: | ---: | --- |
| Initial structural telemetry | 3.14.0rc1 | 4 | 330496 | 344704 | Four rings and a 1024-record epoll-event array. |
| Removed synchronous ring and bounded event array | 3.14.0rc1 | 3 | 324032 | 333184 | Completion-driven receive/send only; event array reduced to the 64-record turn budget. |
| Stable interpreter rebuild | 3.14.6 | 3 | 324032 | 333184 | Forced native rebuild after moving from the release candidate; native eventfd wake completion included. |
| Unified completion ring | 3.14.6 | 1 | 317504 | 333184 | Listener, receive, send, cancellation, and eventfd wake share one 512-entry ring and tagged CQ. |
| Ring-owned signal and timed wait | 3.14.6 | 1 | 317504 | 328320 | Signal socket moved to the control CQ; HTTP/1 uses EXT_ARG timer waits and lazily allocates no epoll registry/event array. |
| Zero-copy send ownership | 3.14.6 | 1 | 317504 | 361088 | SEND_ZC notification state adds eight fixed bytes per operation slot; this 32768-byte cost prevents payload release before kernel ownership returns and is a future packing target. |
| Generic readiness on the unified CQ | 3.14.6 | 1 | 317504 | 361088 | HTTP/3 UDP and other reader/writer registrations use tagged poll SQEs; the epoll fd, event array, control calls, and wait branches were deleted with no idle-capacity growth. |
| Packed zero-copy ownership | 3.14.6 | 1 | 317504 | 328320 | Notification count, terminal state, and errno reuse the operation slot's inactive free-list word while the slot is live, recovering 32768 bytes without shortening kernel payload ownership. |
| Packed send cursor | 3.14.6 | 1 | 317504 | 295552 | The live operation slot's related field carries the immutable payload cursor and the owning transport carries the connection token; only one payload pointer per operation remains in the side table, recovering another 32768 bytes. |
| Transport-owned send payload | 3.14.6 | 1 | 317504 | 262784 | The one-active-send invariant moves the retained payload pointer onto each live transport, deleting the fixed 4096-pointer side table and recovering another 32768 idle bytes while preserving notification lifetime. |
| Packed generational slots | 3.14.6 | 1 | 317504 | 197120 | Live state and operation kind share the high three generation bits, reducing every connection, operation, and buffer-descriptor slot from 32 to 24 bytes and recovering 65664 bytes. Kind zero is encoded as one so connection slots remain live. |

For every new row, record the reason for growth or reduction. Intentional growth
must identify the ownership feature buying those bytes. The focused reactor tests
also enforce close-time mapping release, absent-by-default trace memory, tuned
arena scaling, and one-request SQE/submission/blocking-entry bounds.

A fresh-process comparison records full `VmSize`, `VmRSS`, `RssAnon`, `RssFile`,
Python traced heap, and native-owned mmap/heap/ring counts for `wreath`,
`wreath-native`, and `wreath-metal`:

```console
uv run pytest tests/reactor/test_metal_tier.py -k process_memory_comparison -s -q
```

The test prints all three rows, bounds each process against runaway RSS/heap growth,
and asserts that only `wreath-metal` owns the unified native ring and fixed native
arenas.

Fresh-process checkpoint on CPython 3.14.6 after unified-ring consolidation:

| Mode | VmSize | VmRSS | RssAnon | RssFile | Python traced heap | Native mmap | Native heap | Rings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wreath` | 54431744 | 29642752 | 17235968 | 12406784 | 2860712 | 0 | 0 | 0 |
| `wreath-native` | 54546432 | 29949952 | 17375232 | 12574720 | 2953283 | 0 | 0 | 0 |
| `wreath-metal` | 55062528 | 30068736 | 17567744 | 12500992 | 3184587 | 317504 | 197120 | 1 |

These process values are a regression checkpoint, not a performance claim; loader
layout and allocator state can move them, while the native-owned structural counts
above are expected to be deterministic for a fixed build and kernel feature set.

A separate single-request work-displacement check counts Python interpreter call
events for equivalent HTTP/1 requests without timing them:

```console
uv run pytest tests/reactor/test_metal_tier.py -k request_work_comparison -s -q
```

CPython 3.14.6 checkpoint:

| Mode | Phase | Python | asyncio | Wreath | Python/native accept | Handles | Futures | Tasks | SQEs/enters | Receive/direct CQEs | Send CQEs |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wreath` | fresh | 298 | 110 | 3 | 2/0 | 9 | 4 | 1 | 0/0 | 0/0 | 0 |
| `wreath-native` | fresh | 305 | 100 | 20 | 2/0 | 8 | 4 | 1 | 0/0 | 0/0 | 0 |
| `wreath-metal` | fresh | 170 | 17 | 4 | 0/1 | 2 | 3 | 1 | 3/4 | 1/1 | 1 |
| `wreath` | keep-alive | 23 | 19 | 0 | 0/0 | 4 | 0 | 0 | 0/0 | 0/0 | 0 |
| `wreath-native` | keep-alive | 25 | 19 | 2 | 0/0 | 4 | 0 | 0 | 0/0 | 0/0 | 0 |
| `wreath-metal` | keep-alive | 4 | 2 | 0 | 0/0 | 2 | 0 | 0 | 3/3 | 1/1 | 1 |

The phases are snapshots from one three-request connection, not subtraction
between independent processes. Every measured phase activates the app exactly
once and emits the same 119-byte response. Metal now activates accepted plaintext
connections from the CQ without a Python accept callback, commits HTTP/1 ingress
directly into parser storage, retains immutable sends, batches SQ/CQ publication,
and combines all keep-alive submissions with blocking enters. This quantifies
interpreter and kernel-boundary work moved into native ownership, not elapsed
cost: call events and syscalls have unequal prices, and throughput or latency
claims still require the repeated benchmark policy.
