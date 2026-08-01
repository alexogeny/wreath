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

## Replaying a durable job attempt

The same command, a second record kind. `wreath replay to-test` dispatches on
the container magic, so a `.wfr1` job attempt goes through it without being
announced — what changes is that the target names the **job runner** the task is
registered on rather than an ASGI application:

```console
$ wreath replay to-test herd.app:jobs work-4171-4.wfr1 -o tests/test_incident.py
```

A failed durable job is the thing that actually wakes people, and it is far
harder to reproduce than a request: the request that caused it succeeded hours
ago, the arguments came from state that has since changed, and the failure is on
attempt 4 after two retries and a lease expiry. What
[`wreath.recording`](recording.md#recording-a-durable-job-attempt) writes down is
identity, cause, boundaries and outcome; `replay_attempt` re-runs the registered
handler with every one of those boundaries doubled.

**The join between the two is the coordinate space.** A recorded `BoundaryEvent`
is `(seam, target, coordinate)` — exactly what an `AdapterFaultDescriptor`
addresses — so a boundary that raised becomes an injected fault, and no payload
passes between the recording and the replay. A boundary the attempt crossed
*successfully* contributes no fault; the double answers with its empty default.
A recorded error type with no modelled fault that produces it is **refused by
name**, because injecting the nearest one would replay a different failure and
report it as a reproduction.

**A replay never touches the queue.** It does not claim, complete, fail, sweep,
or enqueue: it calls the registered handler directly, and the runner's own
database is replaced with a double for the duration, so even a handler that
reaches for the queue table arrives at a double instead. That is the property
that makes it safe to point at a production recording on a developer's machine,
and it is asserted against a real PostgreSQL in
`tests/jobs/test_attempt_capture_live.py`.

**The arguments are not in the recording** — see
[why](recording.md#the-arguments-are-not-captured-and-that-is-structural) —
so `replay_attempt` refuses to run with the wrong arity rather than inventing
values. Supply them from the queue row or from a fixture; the generated test
says so in its docstring.

A recorded *failure* generates a test asserting the raise: the outcome, the
error type, its message, and the boundary events that produced it, written into
the file so the doubles are rebuilt the same way on every run. A
characterisation test for a failure is as useful as one for a success and more
often what you want.

::: wreath.replay.recording_kind

::: wreath.replay.open_attempt_recording

::: wreath.replay.replay_attempt

::: wreath.replay.AttemptReplayResult

::: wreath.replay.attempt_fault_schedule

::: wreath.replay.attempt_adapters

::: wreath.replay.generate_attempt_test

::: wreath.replay.AttemptReplayError

## Fault injection

A [`FaultSchedule`](#wreath.replay.FaultSchedule) carries transport faults (keyed
to recorded segment indices) and adapter faults (keyed to the named boundary and
operation index), and round-trips through a checksummed `WFS1` container.
[`fault_corpus`](#wreath.replay.fault_corpus) returns a curated schedule per
taxonomy region — the artifact the sanitizer/fuzz gate re-runs.

The boundaries a fault can reach are the pool, a single lease, the outbound HTTP
client, the LISTEN/NOTIFY doorbell, the transaction scope, a `RETURNING` claim,
and object storage. `fault_corpus`'s own docstring lists every region and says
what makes it one; the paragraphs below say why the less obvious ones are
separate regions rather than variations on their neighbours.

**A region has to name a failure the owned code answers differently.** That is
the bar, and most of these exist because two failures that look alike want
opposite recoveries:

- `connection_drop` fails one statement and leaves the lease usable, so a caller
  may retry on the connection it already holds. `connection_failed` ends the
  lease and *latches* — every later operation on it raises the identical error
  object, and retrying can only produce the answer it already has. Code that
  cannot tell them apart spends its whole attempt budget re-issuing into a
  connection that is gone.
- `server_error` is something PostgreSQL reported. `decode_error` is the
  statement succeeding and the *answer* being unreadable, and it is modelled as
  a `ValueError` rather than a `PostgresError` because that is what it was:
  `text-format array decoding is not supported`, raised by the driver on a cold
  catalog path in a default configuration. Every `except PostgresError` in this
  tree steps around it. It is also the region that pairs with the no-hang
  property — the failure is in the code that resolves a caller's future, which
  is exactly how a printed error and a permanent wait coexist.
- `prepared_poison` is the only region whose failure does not exist on the first
  call. PostgreSQL infers a parameter's type on first execution and the prepared
  statement carries the inference, so the second execution binds by an OID
  nothing can encode. A smoke test that runs each statement once cannot see it,
  and reconnecting does not clear it, so "it worked when I tried it" is true and
  useless.
- `notify_stream_end` and `notify_stream_error` are separate because
  `Connection.notifications()` *returns* when its connection closes rather than
  raising, so a supervisor written around `except` sees nothing at all.
- `claim_lost` is a quiet success: an `INSERT ... ON CONFLICT ... RETURNING`
  that returns no row. Nothing fails, and a caller that reads "no error" as "I
  hold the claim" runs its critical section twice.

On the transport side, `SPLIT` is the odd one out and deliberately so. Every
other transport region removes or reorders bytes and asserts the degradation is
handled; `SPLIT` removes nothing — it moves the *read boundary* into the middle
of a frame — so its assertion is **equality** with the unfaulted replay. That is
the property an incremental parser breaks first, and no "handled it gracefully"
outcome can hide a violation of it.

### Two properties the whole corpus is held to

Every schedule is driven against every subsystem that can reach its seams, and
both of these are checked for all of them (`tests/test_replay_corpus_properties.py`,
with the drivers in `tests/_replaydrive.py`):

- **No fault may produce a hang.** Every drive runs under a wall-clock bound and
  the failure names the schedule *and* the driver. A fault may fail, degrade, or
  be handled; it may never leave a caller waiting.
- **No fault may produce silence.** At least one driver's observation must
  differ from that driver's own no-fault control — an exception, a counter that
  moved, a status that changed. Not *every* driver: a silent fault is
  legitimately silent at a seam with nothing to observe it with. What is never
  legitimate is a fault no owned code anywhere reacts to.

### The containers are untrusted input

Both `WTR1` and `WFS1` recover a torn tail by design, and both refuse a chunk
tag that appears twice — a second copy would silently replace the first while
every checksum still verified, so a recording could be made to replay bytes
nobody recorded.

Beyond that the two deliberately part company, because they are for different
things. A **recording** recovers a truncated tail: the writer appends, a capture
cut short by a crash still holds the forensic material before the tear, and
throwing that away to punish the tear helps nobody. A **schedule** refuses one.
Its whole promise is that two runs got the same injection, and a torn `ADPT`
chunk would drop every adapter fault while the transport half still decoded —
a weaker schedule running under a stronger one's name, in a run that stays
green. For the same reason a schedule names its chunk vocabulary and refuses a
tag outside it: a chunk's tag is not covered by its CRC, so one flipped bit
turns the optional `ADPT` chunk into one the reader has never heard of, and the
adapter faults vanish without a word.

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
