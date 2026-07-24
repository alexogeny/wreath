# Fuzz your own routes

Replay lets you drive your application through Wreath's own protocol and endpoint
code with no socket, no database, and no upstream — and then perturb it along the
seams the framework owns. That makes it a fuzzing harness for *your* routes: feed
adversarial bytes, cut the connection mid-request, make a query fail after it
started, and assert your app still ends in a sane, deterministic state.

Everything here is in-process and side-effect free. It never opens a port, never
touches a real database, and cannot broaden a capture policy.

## Replay a request through your real handler

Start from a canonical request — method, path, headers, body — and run it through
the whole owned pipeline (routing, binding, validation, auth requirements,
serialization). The handler runs for real:

```python
from wreath.replay import CanonicalRequest, replay_endpoint_plan

result = await replay_endpoint_plan(
    app,
    CanonicalRequest("POST", "/orders", headers=((b"content-type", b"application/json"),),
                     body=b'{"sku": "abc", "qty": 2}'),
)
assert result.status == 201
```

`result` carries the owned `status`, `headers`, and `body`. Because a real
handler is arbitrary Python, the run is labelled `best_effort` — the pipeline
around it is real, but the result is only as reproducible as your handler.

## Fuzz the binding and validation layer

Point malformed bodies and odd headers at a route and assert the owned validation
turns them away with a `422`, never a `500` or a crash:

```python
import pytest

BAD_BODIES = [b"", b"{", b'{"qty": "not-a-number"}', b"\xff\xfe", b'{"qty": 1e400}']

@pytest.mark.parametrize("body", BAD_BODIES)
async def test_bad_bodies_are_rejected_cleanly(body):
    result = await replay_endpoint_plan(
        app,
        CanonicalRequest("POST", "/orders",
                         headers=((b"content-type", b"application/json"),), body=body),
    )
    assert result.status in (400, 422)  # an owned rejection, never a 500
```

This is a fuzzing loop: generate inputs (by hand, from a corpus, or from a
property-based strategy) and assert an *owned* outcome. A crash or a `500` is a
finding.

## Fuzz the wire: transport replay with faults

Transport replay drives the real HTTP/1 parser over a fake transport. Record the
bytes a request would send, split them however you like, then inject faults that
truncate, short-read, reset, or half-close the stream:

```python
from wreath.replay import (
    record_transport_segments, replay_transport,
    FaultSchedule, FaultDescriptor, FaultKind,
)

# The bytes of one request, split into three segments (fault coordinates).
raw = b"POST /orders HTTP/1.1\r\nHost: x\r\nContent-Length: 8\r\n\r\n{\"qty\":2}"
recording = record_transport_segments([raw[:20], raw[20:40], raw[40:]])

# Reset the connection after the second segment.
schedule = FaultSchedule((FaultDescriptor(int(FaultKind.RESET), segment_index=1),))

a = await replay_transport(app, recording, faults=schedule)
b = await replay_transport(app, recording, faults=schedule)

assert b"HTTP/1.1 200" not in a.response   # a cut-off request never "succeeds"
assert a.matches(b)                        # same recording + schedule => same outcome
```

The fault kinds are keyed to stable coordinates — the Nth segment, a byte offset
within it — so a schedule is bit-for-bit reproducible. The transport kinds are:

| Kind | What it does |
| --- | --- |
| `SHORT_READ` | deliver only the first `value` bytes of a segment |
| `TRUNCATE` | drop this segment past `value` and suppress every later one |
| `RESET` | inject a peer reset (abort) after the segment |
| `HALF_CLOSE` | inject a peer half-close (read EOF) after the segment |
| `CLOCK_JUMP` | advance the virtual clock by `value` µs before the segment |
| `DUPLICATE` | feed this segment's bytes twice (peer retransmission) |
| `TIMEOUT` | fire the owned request/keep-alive timeout after the segment |

A good sweep: split a recorded request finely, then truncate at *every* offset and
assert the parser never crashes, never fabricates a `200`, and gives the same
answer twice. That is exactly the property a parser must hold, and it is one loop.

## Fuzz the database boundary

Install a `DatabaseDouble` for a route's `FromDatabase` connection and script it —
return rows, or raise a modeled fault at acquire time or on the Nth query. The
double counts acquisitions and releases, so you can prove the framework returned
the connection to the pool even on the error path:

```python
from wreath.replay import (
    replay_endpoint_plan, CanonicalRequest,
    ReplayAdapters, DatabaseDouble, AdapterFault,
)

double = DatabaseDouble("main", query_faults={0: AdapterFault.SERVER_ERROR})

result = await replay_endpoint_plan(
    app, CanonicalRequest("GET", "/users"),
    adapters=ReplayAdapters(databases={"main": double}),
)

assert result.status == 500       # the owned error mapping
assert not double.leaked          # the framework released the connection
```

The database fault kinds model the failures worth rehearsing: `POOL_TIMEOUT`,
`POOL_EXHAUSTED`, `SERVER_ERROR`, `CONNECTION_DROP`, `LOST_COMMIT` (ambiguous
completion), and `RELEASE_ERROR`. Assert two things every time: the owned status,
and `not double.leaked`. Release is *framework*-owned — even a handler that
catches the error and returns `200` should leave `double.leaked` false.

## Fuzz the outbound HTTP boundary

`FaultyHttpClient` is a real `HTTPClient` whose transport seam injects faults, so
the client's own timeout and error handling runs on the real code path. Point a
handler at one and assert your app degrades the way you intend:

```python
from wreath.replay import FaultyHttpClient, AdapterFault

upstream = FaultyHttpClient("api", request_faults={0: AdapterFault.READ_TIMEOUT})

@app.get("/proxy")
async def proxy(request):
    reply = await upstream.request("GET", "/thing")   # times out
    return {"upstream": reply.status}

result = await replay_endpoint_plan(app, CanonicalRequest("GET", "/proxy"))
assert result.status == 500   # your handler didn't catch it -> owned 500
```

Swap the fault for a scripted `ClientResponse` to rehearse the happy path, or a
`CONNECT_ERROR` to rehearse a hard-down dependency.

## The pattern

Whatever the surface, the loop is the same:

1. Describe an input — a canonical request, a recorded connection, a database
   script, an outbound response.
2. Perturb it along an owned seam with a fault.
3. Assert an **owned** outcome: a status, a terminal disposition, a released
   connection, and — always — that a re-run gives the same answer.

A crash, a leaked connection, a fabricated success, or a non-deterministic result
is a finding. Keep the interesting schedules as regression fixtures; a discovered
failure becomes a permanent test. For the framework's own baseline of these,
`tests/test_replay_faults.py` red-teams the parser, the pool lifecycle, and the
outbound path — copy its shape for your routes.
