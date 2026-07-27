# `wreath.replay`

Re-drive Wreath's *own* behavior from a recording — the parser, the framing, the
routing, the binding, the serialization, the connection lifecycle — without a
real socket, a real database, or a real upstream. Replay reproduces what the
framework owns; it never claims to reproduce a kernel, a TLS stack, or arbitrary
Python.

There are two surfaces:

- **Transport replay** feeds recorded inbound byte segments, their virtual
  arrival schedule, and connection-lifecycle events into the real protocol
  driver over a fake transport. It proves the owned parser, framing, and
  response encoding are reproducible. For HTTP/1.1 (:func:`replay_transport`)
  variable response fields (`Date`) are normalized before comparison; for
  HTTP/2 (:func:`replay_transport_h2`) the frames the server wrote back are
  decoded into per-stream responses, so comparison never depends on HPACK byte
  layout or the `date` value. The same byte-level fault kinds apply to both.
- **Endpoint-plan replay** starts from a canonical semantic request and runs the
  owned routing, binding, validation, auth-requirement evaluation, and
  serialization. The handler is invoked, skipped, or replaced with a recorded
  result depending on the mode.

**HTTP/3** is deliberately *not* a transport-replay target. QUIC encrypts every
datagram with per-connection keys negotiated in a fresh TLS handshake, so recorded
datagrams cannot be decrypted by a different server instance — byte-level replay
would require pinning the entire TLS/QUIC RNG, which the guarantees below
explicitly exclude. HTTP/3 reaches parity a different way: **endpoint-plan replay**
is transport-agnostic (an HTTP/3-origin canonical request replays through the exact
same owned pipeline), and the owned HTTP/3 request handling is red-teamed with a
real QUIC client in `tests/http3/`. Forensic **capture** works identically on
HTTP/1, HTTP/2, and HTTP/3, since a Wreath app dispatches all three through the
same owned request context.

On top of both sits **fault injection**: a small, checksummed schedule that
perturbs a compatible recording along owned seams — a short read, a truncated
stream, a mid-message reset, a request-deadline timeout, a database pool timeout,
an outbound connect failure — so the owned *recovery* behavior can be exercised
and asserted deterministic. Faults drive the *real* owned mechanism, never a
simulated outcome: a `TIMEOUT` fault, for instance, fires the protocol driver's
own armed request/keep-alive deadline enforcement (the native `_replay_fire_timeout`
→ `enforce_deadline` in C, mirrored by the pure twin), so an incomplete
body-awaiting request emits a genuine `408` from the same code the live server
runs. Fault injection is replay/test-only: it runs over fake transports and
injected adapters, never a real resource, and cannot broaden any capture policy.

For a hands-on guide to pointing all of this at your own routes, see the cookbook
recipe [Fuzz your own routes](../cookbook/recipes/fuzz-your-routes.md).

## Transport replay

::: wreath.replay.TransportRecording

::: wreath.replay.TransportSegment

::: wreath.replay.SegmentKind

::: wreath.replay.record_transport_segments

::: wreath.replay.replay_transport

::: wreath.replay.TransportReplayResult

::: wreath.replay.replay_transport_h2

::: wreath.replay.H2ReplayResult

::: wreath.replay.open_recording

## Recording to regression test

An incident produces a recording, and a recording is only useful while someone
is looking at it. `wreath replay to-test` transcribes one into a runnable
pytest, so the case survives the incident:

```console
$ wreath replay to-test herd.app:app herd-incident.wtr1 -o tests/test_incident.py
```

The request is parsed out of the recorded bytes, replayed **now** through
[`TestClient`](testing.md), and what comes back becomes the assertion. So the
result is a *characterisation* test: generated against the broken build it
encodes the bug (watch it fail, fix, update the expectation); generated after
the fix it locks the fix in. The tool cannot tell which you meant, and the
generated docstring says so.

Deliberately narrow: request line, headers, and a `Content-Length` body. A
chunked body or a truncated tail is **refused by name** rather than guessed at,
because a generated test that asserts a mis-decoded body is worse than no test.

A recording of a keep-alive connection that carried **more than one request** is
refused for the same reason. The extra bytes would otherwise be dropped past
`content-length` without a word, and you would get a test covering the first of
two requests while looking like it covered the recording. Re-record a single
request, or replay the whole connection with `wreath replay transport`.

::: wreath.replay.recorded_request

::: wreath.replay.generate_test

## Fault injection

A [`FaultSchedule`](#wreath.replay.FaultSchedule) carries transport faults (keyed
to recorded segment indices) and adapter faults (keyed to the named boundary and
operation index), and round-trips through a checksummed `WFS1` container.
[`fault_corpus`](#wreath.replay.fault_corpus) returns a curated schedule per
taxonomy region — the artifact the sanitizer/fuzz gate re-runs.

The boundaries a fault can reach are the pool, the outbound HTTP client, the
LISTEN/NOTIFY doorbell, the transaction scope, a `RETURNING` claim, and object
storage. Two of those regions exist because of failures that shipped: a
notification stream *ends* rather than raising when its connection closes, and a
`RETURNING` claim can come back with no row — both are quiet successes, and code
written to catch exceptions sees neither. `fault_corpus`'s own docstring lists
every region and says what makes it one.

::: wreath.replay.FaultSchedule

::: wreath.replay.FaultDescriptor

::: wreath.replay.FaultKind

::: wreath.replay.AdapterSeam

::: wreath.replay.AdapterFaultDescriptor

::: wreath.replay.fault_corpus

## Endpoint-plan replay

::: wreath.replay.CanonicalRequest

::: wreath.replay.PlanMode

::: wreath.replay.replay_endpoint_plan

::: wreath.replay.PlanReplayResult

## Boundary adapters

Request-scoped doubles that let an `INVOKE` plan replay reach the PostgreSQL,
outbound-HTTP, and object-storage boundaries deterministically — or under an
injected fault, so the framework's owned error mapping and resource release run
for real.

A `DatabaseDouble` also serves the subsystems that hold a connection rather than
borrowing one per request: pass it to a `MessageBus` and its `listen`,
`notifications` and `transaction` seams are faultable, so a supervised reconnect
can be *proved* rather than trusted.

::: wreath.replay.ReplayAdapters

::: wreath.replay.DatabaseDouble

::: wreath.replay.FaultyHttpClient

::: wreath.replay.ObjectStoreDouble

::: wreath.replay.AdapterFault
