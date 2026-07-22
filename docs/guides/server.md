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

## Selecting the metal HTTP/1 operation backend

The explicitly selected metal loop owns a separate HTTP/1 socket-operation path.
Its per-worker runtime uses bounded generational connection and operation slabs;
every recv/send completion validates both the operation token and its connection
token before delivery. The stock asyncio and uvloop transports used by
`wreath-native` do not enter this path and retain their existing behavior.

Metal defaults to the direct epoll readiness adapter. An ordinary io_uring
recv/send arm is available as an explicit experiment:

```python
from wreath.reactor import metal_event_loop

loop = metal_event_loop(worker_id=0, io_backend="io_uring")
```

The equivalent process-level selection is `WREATH_METAL_IO=io_uring`. Ring setup
failure is reported as `OSError`; explicit selection never silently falls back to
epoll. The io_uring arm owns plaintext listener accept. It requests multishot accept,
falls back to one-shot accept when the kernel rejects that opcode, and drains
accepted descriptors from the CQ in bounded batches. Accepted descriptors are
validated against the listener generation before connection construction, so a
completion from a closed or reused listener is discarded.

Plaintext socket ingress uses a separate receive ring with sixteen registered
16-KiB provided buffers. Wreath requests multishot receive, recycles each selected
buffer only after protocol delivery, and falls back to one-shot receive if the
kernel rejects multishot. CQ draining is capped at 64 completions per loop turn.
Connection tokens are generation-validated before touching a transport; stale
completions recycle their buffer without protocol delivery. `pause_reading()`
cancels the outstanding receive and `resume_reading()` rearms it, preserving
asyncio flow-control behavior with a bounded cancellation race. If provided-buffer
registration is unavailable, that loop retains the existing epoll receive path.
Socket sends still use the synchronous operation wrapper.

Each loop is one ownership domain. `worker_id` labels that domain and operations
from another OS thread are rejected rather than reaching its connection table.
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

Workers share TCP and UDP listeners through `SO_REUSEPORT`. `SIGTERM` and
`SIGINT` stop accepting and use the configured shutdown timeout to drain active
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

**Reference:** [`wreath.server`](../reference/server.md).
