# Native server and protocols

Wreath is a normal ASGI application, so it will happily run behind Uvicorn or any
other server you already trust. But it also carries its own, and this is where
the "native" in Wreath earns its keep: an HTTP/1.1, HTTP/2, and optional HTTP/3
server that moves the parsing and dispatch hot path into C, on top of an asyncio
(or uvloop) transport.

## User story: use every core behind one port in production

> *As an API author, I want to use every core in production — several worker
> processes behind one port — without a separate process manager or an nginx out
> front to fan connections out.*

```console
wreath run example:app --loop metal --workers 4
```

The metal loop runs one independently owned io_uring event loop per worker, and
the workers share a single `SO_REUSEPORT` listener group, so the kernel balances
connections across them. Multiworker mode is metal-only and requires a fixed
port; on `SIGHUP` it brings up a complete replacement generation and only drains
the old one once every child reports ready.

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

`max_body_chunks` separately caps parser and application-wakeup work at 4,096
non-empty body units per request: HTTP/1 chunks, HTTP/2 DATA frames, or HTTP/3
DATA callbacks. A byte limit alone cannot distinguish one large body unit from
thousands of one-byte units. Exceeding either limit rejects the request (413 on
HTTP/1, a stream error on HTTP/2 or HTTP/3). Set it with `--max-body-chunks`,
`WREATH_MAX_BODY_CHUNKS`, or `ServerConfig(max_body_chunks=...)`; HTTP/1's
terminating zero-size chunk does not count.

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

### Ablating the metal runtime

Metal's three optional mechanisms each have an environment switch, so any of
them can be measured against its own absence without a rebuild. A tier that
cannot be ablated cannot be defended.

| Switch | Off restores | Measured |
| --- | --- | --- |
| `WREATH_METAL_ASYNC_SEND=0` | one `send()` per response instead of an io_uring SEND | Async wins the median at every response size from 16 B to 256 KB; the p99 differences flip sign with no relation to size. Async is the right default — a size threshold was proposed, measured, and found to have no crossover. |
| `WREATH_METAL_ADAPTIVE_POLL=0` | blocking waits only, no userspace spin | p99 0.091 ms off against 0.092 ms on: the spin is not a tail contributor either way. |
| `WREATH_METAL_GC=stock`, `WREATH_METAL_GC_FREEZE=0` | CPython's automatic collector, traceable startup heap | Neither moves a saturated benchmark: no collection occurs on the request path at all (see below). |

Each was added because something looked like it might be costing tail latency.
In all three cases the measurement said no, which is the point of having the
switch — the alternative is a runtime carrying optimizations nobody can
disprove.

### Pre-arming the first request

The first request a process serves costs multiples of the steady state: cold
interpreter paths, the first parse, the first timer arm, the accept path's first
trip through Python. On the metal loop that measures ~2.1 ms against a ~0.07 ms
steady state — and because the loop is single-threaded, everything arriving
alongside that request waits behind it, so eight simultaneous connections all
see ~2.8 ms rather than one seeing it and seven seeing normal.

`ServerConfig(prearm=N)` drives N synthetic connections through the server's own
listener after it binds and before `serve()` returns. Measured with four:
first-request latency drops from ~2.1 ms to ~0.5 ms for ~2 ms of extra startup.
Almost all of the win is in the first connection; past that the numbers are
noise, and more than a handful only lengthens boot.

Each pre-arm request asks for `Server.PREARM_PATH` — a path no route can match —
so the response is the framework's own 404 and **none of your handlers run**.
That is not a limitation: warming ingress, parsing, routing and egress is where
the cost is, and a guaranteed miss measures the same as a route hit. Everything
else about them is ordinary, so global middleware does see them and metrics or
rate-limit counters will record them. That is why this is opt-in rather than the
default, and why `server.prearmed_connections` reports how many actually
completed — a TLS-only listener or a sandbox without a loopback route leaves a
correct server that simply started cold, and that should be a number you can
read rather than something you infer from a latency graph.

It does not touch the *per-connection* cost. Even against a fully warm process a
brand-new connection's first request runs ~0.25 ms against a ~0.07 ms steady
state, because that connection still constructs its own socket, protocol, and
transport objects and arms its own first deadline. Pre-arming connection four
does nothing for connection five; only pooling those objects would.

### The loop owns its collector

CPython triggers cycle collection from an allocation counter, so on a
request-serving loop it fires wherever the two-thousandth container allocation
lands — inside a request batch. That cost does not show up in a throughput
average; it shows up as tail latency, on roughly one request in a hundred, which
is exactly where a p99 sits. Metal takes the heap over in three separable steps.

`Server` calls `loop.freeze_heap()` as the last step of startup, the one moment
where "everything reachable" and "everything long-lived" are the same set:
modules imported, route table compiled, lifespan run, listeners bound. Those
objects move to the permanent generation, which no collection traverses again.
This is the step that makes collections cheaper rather than merely rarer, and its
cost is that a reference cycle created during startup is retained for the life of
the process. Closing the loop restores the heap: the trigger goes back to
CPython's and the permanent generation is released.

The gen-0 trigger is raised from 2000 to 20000. That does not reduce total
collector time — a young collection costs what the young generation it scans
costs, so rarer collections are proportionally larger — it moves that time off
p99 and onto p999. It is raised modestly rather than removed because it stays the
backstop for a loop that never idles.

Collection then runs from the poller's idle gap, immediately before it waits.
The gate is the arrival EWMA, the same estimator adaptive polling reads, not the
computed block deadline: a saturated loop still computes a multi-second
keep-alive deadline and then returns from the enter immediately, so gating on the
deadline would collect on every batch under full load. Above 250 microseconds of
measured arrival gap the loop runs a young collection; above 20 milliseconds, a
full one; a floor of 10 milliseconds between collections keeps a lightly loaded
server from re-collecting a young generation it just emptied several times per
request. Any collection time is subtracted from the wait that follows, so a
deadline computed before the collection does not slip by its cost, and work a
finalizer scheduled is run rather than slept on. A saturated loop therefore
collects here never, by design, and defers to the raised trigger.

One ownership rule makes this safe, and it is the loop's, not the policy's. A
stock asyncio loop keeps an accepted transport alive through the bound reader it
registered; metal registers no reader, because ingress is an io_uring multishot
receive, so the poller's connection slab owns every live connection outright.
Without that, an accepted connection is a transport-and-protocol cycle reachable
from nothing, and any collection is free to reap it out from under a live socket.
Wreath's own `Server` tracks its protocols and was never exposed; `wreath.postgres`,
`wreath.http_client`, and any third-party `loop.create_server` on this loop were.

Both halves are ablatable without code changes, so the four combinations can be
measured against each other: `WREATH_METAL_GC=stock` returns the heap to
CPython's automatic trigger entirely, and `WREATH_METAL_GC_FREEZE=0` keeps the
startup heap traceable. `loop.gc_stats()` reports the raised threshold, the
frozen object count, and what the policy actually did — idle young and full
collections and the nanoseconds they took — so a run that claims a tail
improvement can say which step earned it.

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

## Cancelling a handler when the client goes away

ASGI's `http.disconnect` is a *message*, not an interrupt. A server that only
queues it stops nothing, because a handler parked on a database query or an
upstream call is not awaiting `receive()` — the work runs to completion against
a socket nobody will ever read. So Wreath's HTTP/1.1 server queues the message
**and** cancels the application task.

**Only for safe methods, by default.** `GET`, `HEAD` and `OPTIONS` are defined
by RFC 9110 as having no intended effect on the server, so abandoning one can
lose nothing but work in progress. Every other method is left to finish. That is
the whole decision, and the reason is what a cancel does *not* undo: unwinding a
`POST` rolls its transaction back cleanly, but the job it enqueued, the card it
charged and the mail it sent all already happened — and the client is gone and
cannot be told which. Wreath draws the same safe/unsafe line
`wreath.policy.idempotency` does.

Declare the exception on the route:

```python
@app.get("/report")                                  # cancelled, by default
async def report(request: Request) -> Response: ...

@app.post("/import", cancel_on_disconnect=True)      # opt in deliberately
async def importer(request: Request) -> Response: ...

@app.get("/audit", cancel_on_disconnect=False)       # opt out deliberately
async def audit(request: Request) -> Response: ...
```

Two boundaries are deliberate:

- **A disconnect after the response has started never cancels.** The status line
  is already on the wire, so aborting there produces a truncated body rather
  than a saved scan. Such a handler still unwinds, at its own next `send`, so
  its cleanup runs where it stands.
- **A WebSocket session is never cancelled.** It observes its own
  `websocket.disconnect`, which it *is* reading, so the message is the mechanism
  there and an interrupt would be a second one.

**What carries this and what does not.** The trigger lives in the HTTP/1.1
protocol — the native one and the Python reference alike — because that is where a
lost connection is observed. HTTP/2 and HTTP/3 multiplex, so a reset stream is
not a lost connection and cancelling the wrong task because a sibling stream
reset would be worse than not cancelling at all; they are not covered yet, and a
route's declaration is simply inert there. On somebody else's ASGI server it is
inert too: nothing in the ASGI spec lets an application ask to be interrupted,
so the handler keeps running exactly as it did before.

The chain past the server is already built. A cancelled task reaching the
PostgreSQL driver sends a wire-level `CancelRequest` on a second connection,
PostgreSQL stops the statement, and the connection returns to the pool usable —
see [the PostgreSQL guide](postgres.md#a-client-that-goes-away-stops-the-query).

## Choosing protocols

HTTP/2 is in the base wheel; HTTP/3 is an explicit install extra. A listener that offers both
`http/1.1` and `h2` negotiates between them over TLS ALPN, so a client gets the
best protocol it supports and older clients still work. HTTP/3 is compiled in
only when you request it, because it pulls in a QUIC stack you should not pay
for unless you use it.

### Installing HTTP/3

```bash
uv add 'wreath[h3]'
# `wreath[http3]` is exactly the same extra under a longer name.
```

The release wheel bundles pinned OpenSSL, ngtcp2 and nghttp3 builds. It neither
uses a distribution's potentially incompatible QUIC stack nor adds a Python
runtime dependency to Wreath.

### Building the HTTP/3 extension from source

Contributors using `WREATH_BUILD_HTTP3=1` need `pkg-config`, nghttp3, and ngtcp2 built against a
QUIC-capable TLS backend. Wreath accepts either ngtcp2 crypto backend —
`libngtcp2_crypto_ossl` (vanilla OpenSSL 3.5 or newer, which is where the QUIC
TLS API landed) or `libngtcp2_crypto_quictls`.

Most distributions do not package a usable combination. Debian trixie, for
example, ships ngtcp2 with only the **GnuTLS** crypto backend, which wreath does
not link against; the OpenSSL one is not packaged at all. Build both libraries
into a local prefix instead — the release tarballs ship a pre-generated
`configure`, so this needs only a C compiler and `make`:

```bash
PREFIX="$HOME/.local/wreath-quic"

curl -sSLO https://github.com/ngtcp2/nghttp3/releases/download/v1.8.0/nghttp3-1.8.0.tar.xz
tar xf nghttp3-1.8.0.tar.xz && cd nghttp3-1.8.0
./configure --prefix="$PREFIX" --enable-lib-only && make -j"$(nproc)" && make install && cd ..

curl -sSLO https://github.com/ngtcp2/ngtcp2/releases/download/v1.25.0/ngtcp2-1.25.0.tar.xz
tar xf ngtcp2-1.25.0.tar.xz && cd ngtcp2-1.25.0
OPENSSL_CFLAGS="-I/usr/include" OPENSSL_LIBS="-lssl -lcrypto" \
  ./configure --prefix="$PREFIX" --enable-lib-only --with-openssl
make -j"$(nproc)" && make install && cd ..

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PKG_CONFIG_PATH"
export LD_LIBRARY_PATH="$PREFIX/lib:$LD_LIBRARY_PATH"
```

Check ngtcp2's configure summary reports `libngtcp2_crypto_ossl: yes` before
building — if it reports `no`, the extension will compile and then fail to load.
The `OPENSSL_*` overrides exist so configure can find OpenSSL without
`pkg-config`; drop them if `openssl.pc` is installed.

Keep the prefix **first** on `LD_LIBRARY_PATH`. A distribution `libngtcp2` with a
matching soname will otherwise satisfy the core library while the crypto backend
comes from your build, mixing two versions across one ABI.

`wreath.server._http3_available()` reports whether the extension *loads*, not
whether it exists. A partial toolchain — the `.so` compiled, a transitive library
absent — reports `False` so `serve()` raises its own actionable error rather than
an `ImportError` from deep in the import machinery. When that happens, `ldd` on
`src/wreath/_native/_http3*.so` names the missing library, and the fix is to
supply it rather than to rebuild.

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

## Serverless without one platform owning the app shape

`LambdaAdapter` translates API Gateway payload v1/v2 and Function URL events
directly into ASGI. Construct one module-scoped instance so one event loop,
connection pools, and ASGI lifespan survive warm invocations:

```python
from wreath.aws_lambda import LambdaAdapter
from myservice import app

handler = LambdaAdapter(app)
```

It preserves repeated v1 headers/query values, v2 cookies, binary bodies and
responses, and exposes the original event/context under the
`wreath.lambda` ASGI extension. Wreath remains an ordinary ASGI app; this is a
deployment adapter, not a server dependency.

Google Cloud Functions uses the same warm-lifespan model through
`GoogleFunctionAdapter`:

```python
from wreath.serverless import GoogleFunctionAdapter

handler = GoogleFunctionAdapter(app)
```

Azure Functions already ships an ASGI host, so `azure_function_app(app)` hands
Wreath to `azure.functions.AsgiFunctionApp` rather than translating the request
again. Vercel's Python runtime also accepts an `app` variable that is an ASGI
application directly—export the Wreath instance, with no adapter at all.

**Reference:** [`wreath.server`](../reference/server.md),
[`wreath.aws_lambda`](../reference/aws_lambda.md).
