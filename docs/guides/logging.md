# Logging

Most logging libraries are a separate world from the rest of your observability.
The logger knows a message; the tracer knows a request; joining them is your
problem, usually solved by threading a correlation id through every function
that might want to say something.

Wreath does not have that seam, because it does not have a separate logger. A
log record here is one 64-byte cell on the ring the Native Flight Recorder
already uses for request completions, published by the same writer, and joined
to its trace by request id when the projector reassembles it. The trace and span
ids were already sitting in the native request context before your handler ran.
Nothing has to be threaded anywhere.

That single decision is what the rest of this guide is downstream of.

## The two tiers

Writing a log line well and writing one quickly pull in different directions, so
there are two ways to do it and the difference is stated rather than hidden.

### The registration tier

A log statement is mostly constant. The message template, the severity, the
field names, their types, and how each should be redacted never change between
calls — only the values do. So declare all of that once, at import, and let the
request path carry only what actually varies:

```python
from wreath import logging as log

DENIED = log.event(
    "auth.denied",
    "user {user} denied access to {resource}",
    level=log.WARN,
    fields=(log.field("user", int), log.field("resource", str, log.RAW)),
)


@app.get("/orders/{order_id}")
async def get_order(order_id: int, viewer: Viewer) -> Order:
    if not allowed(viewer, order_id):
        DENIED(viewer.id, "orders")
        raise Forbidden()
```

The call packs two arguments into a cell and returns. No dictionary, no format
string, no walk up the stack to find the file and line — those came from the
registration. Registration also *validates*: a template naming a field you did
not declare, a declared field the template never uses, or a type the packer
cannot handle all fail at import, which is a much better time to find out than
during an incident.

The `event_name` is not decoration. It is a stable identity for this class of
event, carried through to OTLP as `EventName`, so "how many `auth.denied` today"
is a lookup rather than an exercise in clustering message text.

### The ergonomic tier

For everywhere else, the shape you already expect:

```python
log.info("cache miss for {key}", key=cache_key)
log.warn("retrying {attempt} of {limit}", attempt=n, limit=3)
```

This interns lazily on the template text and works out argument types as it
goes, so it costs a keyword dictionary and a table lookup that the registration
tier does not. It lands in the same ring, renders the same way, and exports the
same way. Use it freely; reach for the registration tier on paths where you have
measured that the difference matters.

## Redaction is deny-by-default

Logging is where secrets leak. A token gets interpolated into a debug message,
the message goes to disk, and the disk goes to a log aggregator with a broader
audience than anyone intended.

So `wreath.logging` follows the same posture as
[`wreath.recording`](../reference/recording.md): a scalar is written as-is, and anything
string-shaped is replaced by a keyed, process-local fingerprint unless you
declare otherwise.

```python
log.info("charging {attempt} via {gateway}", attempt=2, gateway="stripe")
# attempt=2 verbatim; gateway becomes #4f3a1c... — a stable fingerprint
```

The fingerprint still correlates: the same string yields the same value within a
process, so "how many records mention this gateway" survives even though the
gateway's name does not appear. To read a string in cleartext, say so where a
reviewer will see it:

```python
ROUTE = log.event(
    "http.slow", "slow {route}", fields=(log.field("route", str, log.RAW)),
)
```

This does make the ergonomic tier slightly annoying for string-heavy debugging,
and that gradient is deliberate — it pushes anything that really needs cleartext
toward a declaration someone can review.

## Verbose logs that cost nothing until something breaks

The most useful thing in this module is also the oldest idea in it.

`TRACE` and `DEBUG` records made during a request are not published. They
accumulate in a small per-request buffer, and when the request finishes one of
two things happens: if it failed, ran slow, or was explicitly promoted, the whole
buffer is published; otherwise it is discarded and the slot reused.

```python
STEP = log.event("checkout.step", "step {name}", level=log.DEBUG,
                 fields=(log.field("name", str, log.RAW),))

async def checkout(cart: Cart) -> Receipt:
    STEP("validate")
    STEP("reserve_stock")
    STEP("charge")
```

In the steady state those three calls produce no output at all and cost a buffer
append each. When a checkout raises, you get all three, correlated to that
request's trace, showing exactly what led up to it.

This means you can instrument generously — the usual argument against verbose
logging is its steady-state cost, and here there isn't one. For the case the
framework cannot detect, a request that returned 200 and was nonetheless wrong,
promote it yourself:

```python
if total != expected:
    log.set_field("expected_total", expected)
    scope.promote()
```

## The canonical log line

Rather than a scatter of partial lines per request, Wreath emits one wide
structured record: route, plan, protocol, status, error class, timings, trace
and span ids — everything the recorder already knew — plus whatever your code
attached:

```python
async def get_order(order_id: int, viewer: Viewer) -> Order:
    log.set_field("tenant_id", viewer.tenant_id)
    log.set_field("cache", "miss")
    ...
```

```json
{"request_id":7,"route_id":12,"status":200,"duration_us":12400,
 "protocol":"HTTP2","terminal":"OK","trace_id":"...","span_id":"...",
 "attributes":{"tenant_id":42,"cache":"miss"}}
```

Inside a handler the same thing reads more naturally through the request:

```python
request.event.set("tenant_id", viewer.tenant_id)
request.event.promote()          # publish this request's buffered records
```

Outside a configured recorder that accessor returns an inert stand-in, so the
call is always safe and never needs a guard.

One record to find, no join across interleaved lines, and high-cardinality
fields you can query without having pre-aggregated them. For most services this
replaces the majority of hand-written log lines. `log.set_field` works from
anywhere inside the request, including helpers several frames down, and is a
no-op outside one — so a shared helper does not have to know whether it is
serving a request.

Fields follow the same redaction rule as arguments, because a wide event is
exactly where a tenant name and an access token end up side by side.

## Rate limiting, and what it will never suppress

One pathological call site — a cache-miss log in a tight loop, a retry storm —
can drown out everything else. Within each tick the first N records from a site
pass, then every Mth, and the rest are dropped:

```python
from wreath.telemetry import LoggingConfig
from wreath.logging import LogSamplingPolicy
```

```python
TelemetryConfig(
    logging=LoggingConfig(sampling=LogSamplingPolicy(first=100, thereafter=100)),
)
```

**This applies to `INFO` and below only. `WARN`, `ERROR` and `FATAL` are never
sampled.** An error nobody sees is the worst thing an observability system can
produce, so the asymmetry is deliberate and worth remembering.

Nothing is dropped silently. Suppressed records are counted per site and carried
on the next record that gets through, so one line tells you how many like it
were held back:

```text
INFO   cache miss for key=…   (+147 sampled out)
```

## Where the work happens

Everything readable about a record happens somewhere your handler is not:

- The request path packs a cell and returns.
- The projector thread drains the ring, joins each record to its trace, and
  offers it onward.
- A dedicated writer thread renders the template and writes the bytes.

Each hand-off is a bounded queue that drops and counts when full, so a stalled
disk or a slow collector shows up as a rising number rather than as latency in a
handler. Logs get their own writer thread and their own export queue rather than
sharing the projector's, because they outnumber request completions by one to
two orders of magnitude and a log burst must not evict the traces you came for.

Both a text renderer and JSON lines ship from the start — text on a terminal,
JSON everywhere else — so choosing between them is a flag and never a break in
what your pipeline parses.

## Third-party libraries

Wreath does not install itself on the root logger. A framework that seizes
global logging state fights `dictConfig`, surprises anyone with handlers of
their own, and either double-emits or quietly discards their configuration.

So bridging is something you ask for:

```python
import logging
from wreath import logging as log

with log.stdlib_bridge(logging.getLogger("asyncpg")):
    ...
```

Be aware of what the bridge can and cannot do. By the time a `logging.Handler`
runs, CPython has already built a `LogRecord`, walked the stack, and formatted
the message — none of the cost the registration tier avoids has been avoided.
It is the *compatible* path, not the fast one. Its value is that records from
libraries you did not write land in the same correlated stream as everything
else.

The restraint has a cost — two disjoint streams — so it is paired with a check
that notices when that is actually the situation:

```python
from wreath.doctor import check_logging_streams

for finding in check_logging_streams():
    print(finding)
```

## Turning it on

Logging needs a recorder — without one there is no ring for a record to ride and
no projector to correlate it — so it comes up with telemetry:

```python
from wreath.server import ServerConfig, serve
from wreath.telemetry import Mode, TelemetryConfig

config = ServerConfig(
    telemetry=TelemetryConfig(mode=Mode.DETAILED),
    # log_writer=my_collector.write   # defaults to stdout
)
```

That installs the runtime, starts the writer thread, and gives every request a
scope — on HTTP/1.1, HTTP/2, HTTP/3, and for the whole life of a WebSocket
session, which is one recorder context and therefore one log scope.

**Two thresholds, not one.** `level` is what gets published; `capture_level` is
the floor below which a call does nothing at all. Between them a record is
buffered for promotion. They are separate because failure-triggered logging
needs verbose records to be *created* while staying unpublished, and one level
cannot express that:

```python
LoggingConfig(level=log.INFO, capture_level=log.TRACE)   # the useful shape
LoggingConfig(level=log.WARN)                            # capture_level follows
```

Leave `capture_level` alone and it follows `level`, collapsing back to the one
threshold you would expect if you never asked for buffering. With `Mode.OFF` nothing is installed and every `log.*` call stays the
no-op it is before a server boots — and, because the request path checks a plain
module global before it touches anything native, a server with a recorder but no
logging adds no boundary crossings at all.

## Logging from a job, or any other thread

The ring has exactly one writer, and it is the event loop. A record made from a
`wreath.jobs` worker or a thread-pool task therefore cannot go straight onto it:
it is staged, and the loop publishes it on its next tick — up to one writer
interval later, carrying an `off-loop` flag so you can tell a late record from a
reordered one.

You do not have to do anything to get this; a log call is a log call wherever
you make it. What you should know is that the staging queue is bounded, that
overflow is a counted drop rather than growth, and that both numbers are
readable:

```python
log.off_loop_counts()   # {"staged": 41, "dropped": 0, "held": 3}
```

A `staged` that climbs is not an error — it is telling you where your logging
happens. A `dropped` that climbs means a burst outran the loop's drain, and it
is the same `LossReason.LOG_OFF_LOOP` the recorder accounts everything else
with.

## What things cost

Measured on CPython 3.14, 2026-07-28, with
`uv run python -m benchmarks.bench_logging`. One machine — reproduce before
relying on the absolutes; the ratios are the durable part:

| | per record |
| --- | --- |
| `SITE(user, resource)` — the registration tier | **0.42 µs** |
| `log.info("…", **kwargs)` — the ergonomic tier | 1.78 µs |
| a *disabled* `DEBUG(...)` call | 0.07 µs |
| a TRACE/DEBUG record **buffered** for promotion | 2.99 µs |
| structlog, rendering and returning | 2.59 µs |
| `logging.getLogger(...).warning(...)`, `StreamHandler` | 3.90 µs |

Two of these are worth acting on. A **disabled** call is 70 ns, so verbose
instrumentation you leave in the code costs essentially nothing when it is off,
and `if SITE:` stays an escape hatch you will rarely need.

A **buffered** call is not free. Failure-triggered logging holds records as
objects until the request decides whether to promote them, and that is 3.0 µs
apiece — more than an entire small request. If a hot handler carries ten DEBUG
statements and you have set `capture_level` to DEBUG, you are paying ~29 µs per
request for records that a healthy request throws away. Either raise
`capture_level` on that path, or keep the verbose statements where the promotion
is worth their price.

## What is deliberately not here

- **`wreath.audit` is not built on this.** An audit trail needs "never lose a
  record"; application logging promises "never block the request path". Those
  are incompatible, and the audit logger keeps its own path until it gets a
  durability contract designed on its own terms rather than inheriting one.
## Records that survive the process

By default the ring is ordinary memory, so a segfault takes the last records
with it — and those are the ones a post-mortem is about. Give the recorder a
path and it maps the ring from a file instead:

```python
TelemetryConfig(mode=Mode.PULSE, ring_path="/var/lib/myapp/flight.wfrr")
```

The pages then belong to the kernel, so a `SIGSEGV`, a `SIGKILL` or an `abort()`
leaves them intact. Afterwards:

```console
$ wreath flight read /var/lib/myapp/flight.wfrr
ring file /var/lib/myapp/flight.wfrr
  written by pid 4127, worker 0
  ring of 16384 records; head 902, tail 890
  12 recovered, 890 already drained (look for those in the recording's EVNT stream)
  the worker dropped nothing
```

Two things about it are worth knowing before you rely on it.

**It is not durability.** The mapping survives the *process*. It does not
survive a machine losing power before the pages are written back — a clean
shutdown `msync`s, and nothing else does. If you need the second guarantee you
need a different mechanism, and this is not it.

**A full ring drops rather than overwrites**, so if the file says the worker hit
`ring_full`, the records nearest the crash may be exactly the ones missing. The
count is printed first for that reason. The archival `EVNT` stream in a `WFR1`
recording is where the already-drained history lives; the ring file is only
what was still in flight.

The most useful thing in a ring file is often a gap rather than a record: a
completion cell is written when a request *finishes*, so the request that took
the process down is the one with log records and no completion.

If you have a recording of that request, you can ask whether it still does the
same thing:

```console
$ wreath flight replay flight.wfrr checkout.wtr1 myapp:app
request 2 was in flight when the process died
  it had reached 2 log call site(s)
  the replay reached 2
  the replay retraced the whole recorded path
```

It compares the *sequence of log call sites*, so it tells you where a replay
stops matching rather than only whether it did, and exits 1 on divergence — "did
my fix change the path?" is a question you can put in a shell. Run it against
the build that crashed: a site id is import order, not an identity.

Reference: [`wreath.logging`](../reference/logging.md), and
[`wreath.telemetry`](../reference/telemetry.md) for the fixed-size budgets.
