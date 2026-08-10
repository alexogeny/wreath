# Ingest and realtime

Bytes come up a satellite link and go out a browser's `EventSource`. In between
there is one write path, one broadcast, and one decision about what a reader is
allowed to see — applied per frame instead of per key.

## Why the payload is binary

A collar is a battery on a strap, and every byte it sends costs airtime. So does
every byte the field station relays. A one-position batch is **164 bytes** as
compact JSON and **54** as the declaration below — three times smaller, because
protobuf sends a field *number* where JSON sends `"recorded_at_ms":`. Over a
season, for forty collars reporting every twenty minutes, that ratio is the
airtime bill.

[`wreath.protobuf`](../guides/protobuf.md) speaks the wire format with no
third-party package and no code-generation step. A message is a class:

```python
@message
class Position:
    collar_id: int = field(1, kind="uint32")
    recorded_at_ms: int = field(2, kind="sfixed64")
    lat: float = field(3)
    lon: float = field(4)
    accuracy_m: float | None = field(5, kind="float")
    battery_pct: int = field(6, kind="uint32")
    satellites: int | None = field(7, kind="uint32")


@message
class PositionBatch:
    relay: str = field(1)
    positions: list[Position] = field(2)
```

Three choices in there are worth the reading time.

**The field numbers are the contract.** They are written down rather than taken
from declaration order, because a collar potted in resin on a rhino's neck
cannot be redeployed because somebody sorted a dataclass alphabetically.
Reordering the class changes nothing on the wire.

**`| None` means something specific.** `accuracy_m` and `satellites` have
*explicit* presence: a collar that could not estimate its accuracy and one that
estimated it at zero metres are different claims, and proto3's implicit presence
would encode them identically. `battery_pct` is a plain `int`, where the
default *is* the honest reading — a battery reported as 0% is a flat battery.

**`sfixed64` for the timestamp, and `double` kept for the coordinate.** A
millisecond epoch has its high bits set, so a varint spends nine bytes on a
number that fits in eight; `sfixed64` is shorter and faster both ways. The
coordinate does not get the same treatment: `float` would halve it and cost
about 1.7 m of resolution, which is inside the GPS error and therefore
tempting — but the error and the quantisation are independent, so the rounding
is a *bias* the receiver cannot recover and the noise is not. Eight bytes for a
coordinate is the one place this declaration does not economise.

### Reading it in a handler

There is no annotation that binds a protobuf body. `wreath.protobuf` is a codec
and not a content negotiator: nothing maps `application/x-protobuf` to it the
way `application/json` is mapped to the JSON serializer. So the handler reads
bytes and decodes them, which is what
[the recipe](../cookbook/recipes/accept-a-protobuf-body.md) prescribes:

```python
body = await request.body()
try:
    batch = decode(PositionBatch, body)
except ProtobufDecodeError as error:
    raise BadRequest(f"malformed position batch: {error}") from error
```

**The refusal matters as much as the parse, and it matters more here than in
most places.** A satellite relay retries. If a truncated upload answers 500, it
will retry forever at the rate its spool refills, and the endpoint is down for
everybody. Every malformed-input failure — truncation, a length prefix past the
end of the buffer, a varint longer than ten bytes, invalid UTF-8 — raises
`ProtobufDecodeError`, so one `except` covers the lot:

```
truncated -> 400 {"type":"about:blank","title":"Bad Request","status":400,
  "detail":"malformed position batch: a length-delimited field at offset 16
   needs 38 bytes, but only 11 remain"}
```

An **empty** body is not one of those. Zero bytes is a well-formed protobuf
message with every field defaulted, so it decodes to a batch with no relay name
— and the refusal says *that*, rather than "malformed". Getting it the other way
round would mean a station with nothing to send could never say so.

### One bad position is not a bad request

A collar with a corrupt almanac reports a latitude of 91.4 degrees.
`Coordinate` refuses it — correctly — and the handler catches that refusal into
a number rather than a status code:

```python
BatchReceipt(accepted=39, rejected=1, watermark_ms=...)
```

The batch parsed perfectly and thirty-nine other collars were fine. Losing an
entire station's upload to one device is a much worse outcome than dropping the
reading, and the count is what makes the failing device findable — a station
operator sees one collar going wrong rather than discovering it a season later.

The same applies to a `collar_id` nobody has fitted. Without the check it would
be a foreign-key violation, which fails the whole statement, so one stray id in
a two-hundred-position batch would lose the other hundred and ninety-nine.

### The receipt, and idempotency

```
status 200 application/x-protobuf, 11 bytes
BatchReceipt(accepted=1, rejected=0, watermark_ms=1774343400000)
```

`watermark_ms` is the newest `recorded_at` this application now holds, so a
station that restarts sends everything newer than the last watermark it was
given and does not have to remember anything itself. Note that it is the newest
*recorded* time and not the newest *arrival*: a watermark on arrival time would
skip a collar's buffered positions on the resume, silently, and those are
exactly the ones this example is about.

Then the write:

```sql
INSERT INTO tracking.fixes (...) VALUES (...), (...)
ON CONFLICT (collar_id, recorded_at) DO NOTHING
```

**`(collar_id, recorded_at)` is the primary key and there is no `id` column.** A
collar takes at most one position per instant, so the pair *is* the identity —
and that is worth more than tidiness here. A station whose upload times out
after the write has no way to know it succeeded, so it retries the whole batch;
with a synthetic key that retry is a second copy of every position in it. With
this key the retry lands nothing. Ingest is idempotent because of the schema,
not because of a flag somebody remembered to check.

One wrinkle that is only visible once you write it: PostgreSQL refuses
`ON CONFLICT DO NOTHING` for two rows colliding *inside one command* — "cannot
affect row a second time" — so a station that spooled the same position twice
would fail the whole request rather than landing it once. The batch is therefore
deduplicated before the statement. The conflict clause protects against the
retry; nothing but the deduplication protects against the duplicate.

### The same ingest, streamed

A station that dials in, drains its spool and hangs up wants a `POST`. A station
on a permanent link wants neither a request per fix nor a delay while it
accumulates a batch worth sending, and that is what gRPC client-streaming is
for. `tracking/rpc.py` is thirty lines and it adds no ingest:

```python
@service.client_stream(request=Position, response=BatchReceipt)
async def Relay(request, positions) -> BatchReceipt:
    relay = request.header(RELAY_METADATA) or ""
    collected = [p async for p in positions]      # bounded by MAX_POSITIONS
    receipt = await accept(session, PositionBatch(relay, collected), now=...)
    await live.publish(receipt.published)
    return BatchReceipt(...)
```

The point of the stage is the line that is *not* there: there is no second
`accept`. The per-position rejection counting, the deduplication, the leg
repair, the `ON CONFLICT DO NOTHING` retry and the broadcast-after-write
ordering are all the ones stage one already argued about, so a divergence
between the two transports would be a bug rather than a feature. Three
decisions are the transport's own:

- **The relay's name is call metadata, not a field.** `PositionBatch` names it
  once for a whole batch; a stream has no batch to hang it on. gRPC metadata
  arrives with the request headers, before the first message, which is exactly
  when the server wants it.
- **`IngestRefused` becomes `INVALID_ARGUMENT`,** never a retryable code. A
  station told `UNAVAILABLE` retries forever at the rate its spool refills —
  the gRPC form of the 500-instead-of-400 failure above.
- **An empty stream is a zero receipt, not a refusal,** because that is what the
  `POST` path answers an empty batch. Refusing here would have been the second
  ingest this module exists not to be.

**This runs on wreath's own server and nowhere else, and it needs TLS.** gRPC
puts its status in HTTP/2 trailers, which a foreign ASGI server does not expose, and
`serve` negotiates `h2` through ALPN rather than sniffing the first application
bytes — so prior-knowledge `h2c` is unavailable too. A call arriving over
HTTP/1.1 is answered `UNIMPLEMENTED` with a message naming the transport, which
`tests/tracking/test_stream.py` asserts. The REST relay is not going anywhere:
it is what a deployment behind somebody else's ASGI server keeps using, and both
are mounted at once.

## The collar that lost the sky

`Fix.leg_m` holds the distance from the previous fix, and the daily chart sums
it. Storing that derivation means one number per day crosses the wire instead of
a thousand coordinates — and it means the column has to be *maintained*, which
is where late data stops being an abstraction.

A collar under riverine canopy keeps taking positions and uploads them when the
sky comes back. Those rows land **between** rows that are already there. The fix
that used to come straight after the gap now has a leg measured across three
days of walking that has since been filled in, and leaving it would count that
stretch twice.

So the ingest path recomputes legs for the affected animal from the earliest
position that landed, forwards — starting one fix *before* it, which is the only
way to give the first repaired fix a predecessor to measure from. For a live
batch that is a handful of rows. For a week-old buffer dump it is a week of that
animal's fixes, once, which is the honest price of a stored derivation.

**The distance is computed in Python, not in SQL, and that is a decision.**
Writing the haversine formula into the `UPDATE` would give two implementations
of "how far apart", and the stored column and
[`Trajectory`](../guides/geospatial.md) would be free to disagree by a rounding
rule nobody would notice for a year. `tests/tracking/test_ingest.py` asserts
they agree, and they can only agree because there is one of them.

### Yesterday changed. Now what?

The daily view seals:

```python
daily = (
    Series(Fix, at=Fix.recorded_at, bucket=Day, stored_in=zone(timezone))
    .where(Fix.animal_id == Param("animal"))
    .measure(fixes=count(), distance_m=sum_(Fix.leg_m, unit="m"))
    .seal(after="36h")
)
```

Thirty-six hours because that is this programme's field claim: a collar that has
not reported for a day and a half is a collar somebody drives out to check, so a
position older than that is not "late", it is a recovery. That is a statement
about *these* collars under *this* canopy, not a framework default, which is why
it is declared in the application.

Read a day past the horizon and the envelope says it is settled. Reading does
not *store* it — `Series.settle()` does, from a job — so this is an ordinary
`GET` on a read session:

```json
{"days":[{"day":"2026-03-23T21:00:00+00:00","fixes":72,"distance_m":7797.4},
         {"day":"2026-03-24T21:00:00+00:00","fixes":72,"distance_m":9127.1}],
 "sealed_through":"2026-07-30T00:00:00+03:00","corrections":[]}
```

Now the relay uploads a position that belongs to the first of those days. Read
again, and **nothing has changed** — which is correct and is not a bug:

```json
{"days":[{"day":"2026-03-23T21:00:00+00:00","fixes":72,"distance_m":7797.4}, ...],
 "corrections":[]}
```

Nothing notices a late write on the write path. The ORM's write events are
model-grained by design — they publish which models a session touched, not which
rows — so they cannot say which bucket a late fix belongs to. The gap stays
*visible* rather than assumed away until something reconciles:

```python
corrected = await view.reconcile(session, range=window, animal=11)
# ['2026-03-23T21:00:00+00:00']
```

And now the day reads differently, and says so:

```json
{"days":[{"day":"2026-03-23T21:00:00+00:00","fixes":73,"distance_m":7824.4},
         {"day":"2026-03-24T21:00:00+00:00","fixes":72,"distance_m":9127.1}],
 "corrections":["2026-03-23T21:00:00+00:00"]}
```

**The number that matters is not on that page.** It is in the table:

```
         bucket         |                    measures
------------------------+------------------------------------------------
 2026-03-23 21:00:00+00 | {"fixes": 72, "distance_m": 7797.378655104577}

         bucket         |                     delta
------------------------+------------------------------------------------
 2026-03-23 21:00:00+00 | {"fixes": 1, "distance_m": 27.012188408620204}
```

The settled value is **exactly what it was**. The difference is recorded beside
it and folded in on read. That is the whole argument for sealing: a weekly
report went out quoting 7 797 m, and it can still be reconciled against the
system that produced it. If a settled number could change under you, it was
never settled — and a discrepancy somebody finds in a spreadsheet three weeks
later is a much worse way to learn that a card came in late.

`on_late="reopen"` exists and would overwrite the settled value instead. This
example does not use it, and `tests/tracking/test_late_data.py` pins that
choice, because reopening also clears the correction that would have shown
anything was wrong.

## One broadcast, four maps

The live map is Server-Sent Events, not a WebSocket: no client ever sends
anything up this channel, so the browser half is `new EventSource(...)` and
nothing else.

The interesting part is not the transport. It is that two people watching the
same map are not shown the same map, decided by the same Cedar policy set that
guards the REST routes — and there is **one broadcast**.

```python
subscriber = await live.subscribe(precision_grid(request.identity))
```

[`RoomRegistry`](../guides/websockets.md) takes "anything with an awaitable
`send(payload)`; it need not be a `WebSocket`". A `Subscriber` is that anything:
`send` drops the payload on a bounded queue and the SSE generator drains it. So
the cross-worker fan-out wreath ships for chat rooms carries a live map with no
adapter and no second mechanism — a position ingested by worker 3 reaches a
browser connected to worker 1 because the message bus already does that.

Two readers, one broadcast, and what actually goes down the two connections:

```
--- ranger: text/event-stream
: tracking live map
retry: 3000

event: position
id: 3:2026-03-14T09:20:00+00:00
data: {"animal_id":3,"animal":"Nashipae","collar_id":3,
       "recorded_at":"2026-03-14T09:20:00+00:00","battery_pct":74,
       "position":{"lat":-1.9293,"lon":36.0712},"precision_m":0.0}

--- volunteer: text/event-stream
: tracking live map
retry: 3000

event: position
id: 3:2026-03-14T09:20:00+00:00
data: {"animal_id":3,"animal":"Nashipae","collar_id":3,
       "recorded_at":"2026-03-14T09:20:00+00:00","battery_pct":74,
       "position":{"lat":-1.9335401771662395,"lon":36.03832547745748},
       "precision_m":10000.0}
```

Same event, same animal, same instant, same battery. Different position.

`tests/tracking/test_live.py` holds this, and it is the most valuable test in the
example: everything else composes two things that were designed together, and
this composes the realtime fan-out with the authorization ladder. A unit test for
`RoomRegistry` proves a payload reaches a member. A unit test for `degrade`
proves a coordinate coarsens. Neither can prove that one broadcast produces two
different maps.

### Where the degradation happens, and why not earlier

The payload crossing the bus carries the **exact** coordinate. That is
deliberate, and it is the only arrangement that works:

- Coarsening before the broadcast would need one room per grade, so the fan-out
  cost would multiply by the number of grades rather than staying one `NOTIFY`
  per batch. It also puts the policy decision on the *writer's* side, where
  nobody knows who is reading.
- The bus is inside the trust boundary: a PostgreSQL `NOTIFY` on the
  application's own database, reaching the application's own workers. The
  boundary this example defends is between the application and its readers, and
  that boundary is the edge of the response.

### Three smaller decisions in the stream

**One broadcast per batch, one event per position.** A station draining a week's
spool costs the bus one message and the browser two hundred `message` events.
Broadcasting per position would multiply `NOTIFY` traffic by batch size for no
gain.

**A slow reader is counted, not dropped.** `RoomRegistry` treats an exception
from `send` as a dead peer and removes the member from the room — so a
`QueueFull` escaping `Subscriber.send` would silently unsubscribe every reader
who fell one frame behind, and their stream would stay open and empty forever. A
live map that looks like a quiet afternoon. The overflow increments a counter
instead, which is the shape `wreath.messaging` uses for its own dropped work.

**The grid is fixed for the life of the stream.** A reader whose role is revoked
keeps their resolution until their `EventSource` reconnects, which the `retry`
hint puts at a few seconds after any deploy. Re-evaluating per frame would ask
Cedar once per position per reader; the honest fix for a revocation that must
take effect *now* is to close the stream, not to re-decide inside it.

## The rough edges this example hit

Two of them, both worth knowing before you write the same code.

**`Response(media_type=...)` accepts a `str` and emits an invalid header.** The
parameter is annotated `bytes | None` and is not checked, so a `str` goes into
the header list unconverted and the application emits
`(b"content-type", "application/x-protobuf")` — a bytes name with a `str` value,
which is not a legal ASGI header pair. Nothing raises where you wrote it. What
surfaces is a `TypeError` from whatever reads the headers next. The
[protobuf recipe](../cookbook/recipes/accept-a-protobuf-body.md) shows the `str`
form, so anyone following it hits this; `tracking/wire.py` names a
`MEDIA_TYPE_HEADER` constant that is the same string as `bytes`, and says why.

**A sealed `Series` needs one line the other wreath-owned tables do not.** A
`Series` is a declaration built where it is used, so the application never holds
one and `app.schema_components()` had nothing to ask: the settled-bucket tables
were emitted by `wreath schema sql` and created by nothing at all. `app.py` now
says `application.series(database="main")`, which gives the claim an owner and
has the lifespan create the tables the same way it creates the message bus's.

Two rough edges this page used to name are gone with it, and they are worth
recording because both were real. The DDL no longer has to be applied through a
private `wreath._series.settle` import. And **reading a sealed view is no longer
a write**: it used to settle as a side effect, so a chart `GET` needed a
write-workload session and a read session answered `cannot execute INSERT in a
read-only transaction` from inside the series machinery, on a route that wrote
nothing the application can see. `read_daily` takes a `ReadSession` now, and
`Series.settle()` — which `reconcile()` runs first — is the write half.

Next: [Place and policy](place.md), for the queries underneath all of this and
the ceiling they run into.
