# Native server and protocols

Wreath is a normal ASGI application, so it will happily run behind Uvicorn or any
other server you already trust. But it also carries its own, and this is where
the "native" in Wreath earns its keep: an HTTP/1.1, HTTP/2, and optional HTTP/3
server that moves the parsing and dispatch hot path into C, on top of an asyncio
(or uvloop) transport.

The simplest way to run it is from the command line:

```bash
wreath run app:app --host 0.0.0.0 --port 8000
```

From Python you build a validated `ServerConfig` and hand it to `run`:

```python
from wreath.server import run, ServerConfig, TLSConfig

config = ServerConfig(host="0.0.0.0", port=8000, protocols=("http/1.1", "h2"))
run(app, config=config, tls=TLSConfig("cert.pem", "key.pem"))
```

The defaults cap request bodies at 1 MiB and multiplexed concurrency at 64
streams, keeping the nominal per-connection body budget at 64 MiB. Raise either
explicitly only alongside application-consumption flow control and an observed
workload that needs it.

## Bounding HTTP/3 response retention

HTTP/3 may need to retransmit response data, so handing bytes to the QUIC stack
does not immediately release Wreath's reference to them. Wreath retains each
immutable response segment until the peer acknowledges it. Both the stream and
its connection share the configured response-credit limits:

```python
config = ServerConfig(
    protocols=("h3",),
    response_high_water=1024 * 1024,
    response_low_water=512 * 1024,
    response_high_water_segments=1024,
    response_low_water_segments=512,
)
```

An ASGI `send` becomes pending after retained bytes or segments cross a high
watermark. It resumes only after acknowledgement releases both the stream and
connection to or below their low watermarks. The segment limit matters even for
tiny non-empty application chunks, whose metadata still consumes memory. These
are pressure limits rather than a response-size limit: one application-owned body
message is accepted before its `send` can be suspended.

The same settings are available to the CLI as `--response-high-water`,
`--response-low-water`, `--response-high-water-segments`, and
`--response-low-water-segments`.

## HTTP/2 output pressure and fairness

HTTP/2 response DATA needs three kinds of credit at once: the stream flow-control
window, the connection flow-control window, and writable transport capacity. If
the TCP or TLS transport reaches its high watermark, Wreath stops framing new
DATA and leaves the application's `send` pending. Work resumes in a bounded batch
when the transport reports writable capacity again.

When several streams share renewed connection credit, stock `wreath-native` uses
Deficit Round Robin rather than draining the first response in stream order.
Deficits persist between activations, while per-activation stream and byte budgets
keep a large response from monopolizing timers or unrelated streams.

The metal loop layers RFC 9218 policy over that bounded scheduler. It reads the
`Priority` request field and request-stream `PRIORITY_UPDATE` frames, including
updates for idle streams in a table bounded by the configured concurrent-stream
limit. Lower numeric urgency runs before lower-priority bands. Incremental
responses rotate with peers at the same urgency; a non-incremental response keeps
same-urgency ownership while it makes progress, but higher urgency can preempt it.
Every activation remains capped by the existing stream and byte budgets. Legacy
RFC 7540 dependency-tree `PRIORITY` frames remain ignored. This policy is selected
only by metal; stock asyncio and uvloop scheduling is unchanged.

## Metal HTTP/1 native ownership

The metal loop owns its HTTP/1 sockets through io_uring. Its per-worker runtime
uses bounded generational connection and operation slabs; every recv/send
completion validates both the operation token and its connection token before
delivery. There is no metal backend switch and no asyncio/uvloop transport beneath
this path. Ring setup failure is reported as `OSError` rather than changing the
execution architecture. The stock asyncio and uvloop transports belong only to
`wreath-native` and retain their separate behavior.

The io_uring runtime owns plaintext listener accept. It requests multishot accept,
falls back to one-shot accept when the kernel rejects that opcode, and drains
accepted descriptors from the CQ in bounded batches. Accepted descriptors are
validated against the listener generation before connection construction, so a
completion from a closed or reused listener is discarded.

Plaintext socket ingress uses the unified ring with 1024 registered 16-KiB
provided buffers, sized so pool exhaustion coincides with CQ saturation. Each
offered buffer owns a generational descriptor token;
a CQE's kernel buffer ID must claim the current token before protocol code accesses
the memory. Duplicate and stale ownership epochs are rejected in O(1), with slab
occupancy, high-water, exhaustion, stale-token, and wrap diagnostics. Wreath
requests multishot receive, recycles each selected buffer only after protocol
delivery, and falls back to one-shot receive if the kernel rejects multishot. CQ
draining is capped at 64 completions per loop turn. Connection tokens are
generation-validated before touching a transport; stale completions recycle their
buffer without protocol delivery. `pause_reading()`
cancels the outstanding receive and `resume_reading()` rearms it, preserving the
transport flow-control contract with a bounded cancellation race. If the kernel
reports provided-buffer exhaustion, it ends that receive epoch; Wreath records the
exhaustion and rearms only at the bounded post-drain retry point, after selected
buffers have been recycled. If provided-buffer registration is unavailable, metal
loop startup fails instead of changing the transport architecture.

Plaintext TCP output uses `IORING_OP_SEND` on the unified ring. Each send SQE
carries a generational operation token and retains an immutable payload until
its data CQE arrives; `MSG_WAITALL` lets the kernel retry short sends, so one
CQE normally covers the whole payload. Partial
sends advance a cursor and resubmit the remaining bytes; writes that arrive behind
an active send enter the transport's bounded flow-control buffer. Completion releases the
operation slot, resumes a paused producer at the low watermark, and submits the
next payload. `close()` waits for all accepted bytes to complete, while abort and
stale-connection paths discard callbacks without releasing kernel-visible memory
early. Send CQ draining is also capped at 64 completions per loop turn. Failure to
initialize asynchronous send ownership fails metal startup.

Async SQEs are gathered in userspace and submitted once at bounded loop phase
boundaries rather than entering the kernel for every operation. The poller
reports submitted SQEs separately from submission batches.

After an empty completion probe, the adaptive polling controller learns an EWMA
of empty-CQ-to-arrival time and its absolute deviation. It spins only after
eight samples and only below a 100-microsecond predicted arrival gap; the
per-spin budget is the prediction plus twice the deviation, clamped between 2
and 50 microseconds. Every spin
releases the GIL, and falls through to ordinary blocking on a miss. Timers, ready
Python callbacks, and immediate control work bypass spinning. Telemetry exposes
attempts, hits, misses, spin time, blocking entries, samples, EWMA, and deviation.
Cross-thread scheduling writes a native eventfd whose poll request and wake are
owned by io_uring; the completion is drained and rearmed without scheduling the
inherited Python self-pipe callback. The inherited socketpair remains only for
CPython signal wake bytes are polled by that same ring and dispatched through its
control CQ path. Timer blocking uses `io_uring_enter(EXT_ARG)` deadlines derived
from the native wheel and scheduled callbacks. Generic reader/writer readiness,
including the UDP descriptor used by the HTTP/3 datagram adapter, uses tagged
`IORING_OP_POLL_ADD` requests with generation validation, cancellation, bounded
CQ dispatch, and rearm. Metal therefore owns no epoll instance or event array.
Adaptive polling is part of the metal runtime rather than a product selection.
Completion-trace capture is separately opt-in through
`WREATH_METAL_TRACE=1`; the default allocates no trace ring and performs no
per-completion trace write, avoiding diagnostic cache and RSS cost on production
workers.

Each loop is one ownership domain. `worker_id` labels that domain and operations
from another OS thread are rejected rather than reaching its connection table.
Metal allocates its hot ownership tables once, before serving, and never grows
them on the request path. Tune per-worker baseline RSS and concurrency limits with
`WREATH_METAL_CONNECTION_CAPACITY` (default 4096),
`WREATH_METAL_OPERATION_CAPACITY` (default 4096 io_uring in-flight ownership
slots), and `WREATH_METAL_RECV_BUFFERS` (default 1024).
Values must be at least 16; receive
buffer count must be a power of two. Invalid or excessive values fail loop startup
rather than being rounded or triggering hidden dynamic growth. The poller exposes
`native_mapped_bytes`, `native_heap_bytes`, and `native_ring_count` alongside SQE,
submission-enter, and blocking-entry counters. Metal owns one 512-entry io_uring
for listener accept, provided-buffer receive, asynchronous send, cancellation,
and eventfd wake. Two high token bits select the completion class while the lower
62 bits retain the generational ownership payload. Focused tests bound those
values and the kernel-entry deltas for one real HTTP/1 request so RSS or syscall
growth must
be an explicit baseline change.
Set `reuse_port=True` (or `WREATH_METAL_REUSEPORT=1`) when separately managed
metal workers should bind one `SO_REUSEPORT` listener group:

```python
loop = metal_event_loop(worker_id=2, reuse_port=True)
```

For a managed process group, the CLI creates one independently owned loop per
worker and waits for every replacement to finish startup before draining the old
generation:

```console
wreath run example:app --loop metal --workers 4
```

Workers share TCP and UDP listeners through `SO_REUSEPORT`. Set
`WREATH_METAL_AFFINITY=auto` to pin worker `N` deterministically to available CPU
`N % cpu_count` before its metal loop allocates rings and slabs, giving those
worker-owned pages affinity-local first touch. This is the metal default; an
invalid policy or unsupported affinity API fails worker startup rather than
silently changing placement. `WREATH_METAL_AFFINITY=off` exists only for
constrained container operation, not as an alternate scheduler implementation.
`SIGTERM` and `SIGINT` stop accepting and use the configured shutdown timeout to
drain active
work. `SIGHUP` starts a complete replacement generation, waits for an explicit
post-bind and post-lifespan readiness byte from each child, and only then signals
the previous generation. A failed replacement leaves the current generation in
service; an unexpectedly exited current worker is replaced. Multiworker mode is
metal-only and requires a fixed port.

## Choosing protocols

HTTP/2 and HTTP/3 need the native extension. A listener that offers both
`http/1.1` and `h2` negotiates between them over TLS ALPN, so a client gets the
best protocol it supports and older clients still work. HTTP/3 is compiled in
only when the extension is built with `WREATH_BUILD_HTTP3=1`, because it pulls in
a QUIC stack you shouldn't pay for unless you want it.

## Configuring from the environment

The same application should run differently in development and production without
code changes. `ServerConfig.from_env()` layers `WREATH_*` environment variables
over the defaults, and any argument you pass explicitly wins over the
environment. To catch a missing secret before it causes a mysterious failure
mid-request, name your boot-critical variables and Wreath will warn at startup:

```python
from wreath.server import run
run(app, required_env=["DATABASE_URL"])
```

The [Configuration and state](config-state.md) guide covers this in full.

## Metal champion checkpoint

These checkpoints make metal optimization reviewable without treating one timing
sample as evidence. Fixed native ownership is a deterministic regression gate;
process RSS supplements it, and interpreter call events show work displaced from
the Python loop without pretending every call has the same cost.

Reproduce fixed ownership:

```console
uv run python -c 'import sys, wreath.reactor as r; l=r.metal_event_loop(); print(sys.version.split()[0], l._poller.native_ring_count, l._poller.native_mapped_bytes, l._poller.native_heap_bytes); l.close()'
```

Current CPython 3.14.6 champion:

| Rings | Native mmap bytes | Native heap bytes |
| ---: | ---: | ---: |
| 1 | 317504 | 197120 |

Reproduce the fresh-process memory comparison:

```console
uv run pytest tests/reactor/test_metal_tier.py -k process_memory_comparison -s -q
```

| Mode | VmSize | VmRSS | Python traced heap | Native mmap | Native heap | Rings |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `wreath` | 54431744 | 29642752 | 2860712 | 0 | 0 | 0 |
| `wreath-native` | 54546432 | 29949952 | 2953283 | 0 | 0 | 0 |
| `wreath-metal` | 55062528 | 30068736 | 3184587 | 317504 | 197120 | 1 |

Reproduce equivalent-request interpreter work:

```console
uv run pytest tests/reactor/test_metal_tier.py -k request_work_comparison -s -q
```

| Mode | Phase | Python | asyncio | Wreath | Python accept | Native accept | Handles | Futures | Tasks | SQEs | Enters | Receive/direct CQEs | Send CQEs |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `wreath` | fresh connection | 298 | 110 | 3 | 2 | 0 | 9 | 4 | 1 | 0 | 0 | 0/0 | 0 |
| `wreath-native` | fresh connection | 305 | 100 | 20 | 2 | 0 | 8 | 4 | 1 | 0 | 0 | 0/0 | 0 |
| `wreath-metal` | fresh connection | 170 | 17 | 4 | 0 | 1 | 2 | 3 | 1 | 3 | 4 | 1/1 | 1 |
| `wreath` | keep-alive request | 23 | 19 | 0 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0/0 | 0 |
| `wreath-native` | keep-alive request | 25 | 19 | 2 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0/0 | 0 |
| `wreath-metal` | keep-alive request | 4 | 2 | 0 | 0 | 0 | 2 | 0 | 0 | 3 | 3 | 1/1 | 1 |

One subprocess now serves three requests on one connection and records phase
snapshots around the first and second responses; the keep-alive row is no longer
subtraction between separate runs. Each measured phase activates the app once and
emits the same 119-byte response. Metal's fresh path displaces 128 Python calls
versus `wreath` and 135 versus `wreath-native`; its steady keep-alive path uses
four Python call events, with parser ingress committed directly from the receive
SQE. SQ tail and CQ head publication counters, total enters, accepted-connection
activation, and direct-receive ownership are regression gates in the test. This
is structural evidence, not a throughput or latency claim; published performance
claims still require repeated runs that clear the measured A/A noise floor.

**Reference:** [`wreath.server`](../reference/server.md).
