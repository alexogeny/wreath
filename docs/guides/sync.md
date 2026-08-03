# Subscribing to a query

A list that other people can change is one of the oldest problems in web
applications, and the usual answers are both unsatisfying. Polling asks the same
question on a timer whether or not anything happened. A websocket that says
"something changed, refetch everything" is the same poll wearing a nicer
transport. What the client actually wants is narrower and more useful: *this
query's answer changed, and here is the difference.*

Answering that needs something that can see both the query and the write. The
local-first products that grew up around this problem — Zero, Electric,
PowerSync — all sit *beside* the database, and because they live outside the
application, each one has had to rebuild authorization from the outside in. The
gap between what the sync service believes you may see and what the route
believes you may see is the leak the whole category has spent years fighting.

Wreath is already inside the application. A shape is a `Select`, evaluated by
the ORM you already use, under the policies you already wrote.

## A first shape

```python
from wreath.sync import Sync

photos = Sync(Photo, key=lambda row: row.id)

@photos.shape("mine")
def mine(principal):
    return (
        Photo.select()
        .where(Photo.owner_id == principal.sub)
        .order_by(Photo.taken_at.desc())
        .limit(500)
    )
```

That is the whole declaration. `Sync` watches `Photo` for writes; the shape says
which photos this principal may see and in what order; and the `limit` is
load-bearing in a way worth explaining before you write your second one.

## Why a shape must be bounded

`Sync` refuses a shape with no `limit`, and it refuses it at the line that
declared it rather than at the first request that runs it. That is deliberate,
and it is not fussiness about resource use.

An unbounded shape works beautifully against a table with fifty rows in it. It
carries on working through the first year. It stops working — quietly, at three
in the morning, against a table that has since grown to five million rows — long
after the person who wrote it has moved on. Refusing at declaration puts the
error next to the code that has to change, while somebody is still looking at it.

The bound buys something else, too, and it is the reason this feature can make a
promise its competitors struggle with.

## Revocation, which is the hard part

Every product in this category finds the same thing easy and the same thing hard.
Telling a client about a row it has just *gained* is straightforward. Telling a
client about a row it has just *lost* — because the policy changed, because the
row's owner changed, because somebody was removed from a team — is the case that
gets shipped broken, because the happy-path test passes without it.

A bounded shape makes that case ordinary. The answer is at most `limit` rows, so
the subscription can simply hold the key set it last sent and compare:

- keys that arrived are **upserts**
- keys that left are **removals** — tombstones

Revocation is not a special search. It is the same diff, running the same code,
on every evaluation. There is no separate revocation path that could be correct
in your tests and wrong in production because nobody exercised it.

```python
delta = await subscription.poll(session)
delta.upserted   # rows the client should store
delta.removed    # keys the client must drop
```

A client that applies `upserted` and ignores `removed` has kept every row it was
ever revoked. That is worth saying to whoever writes the client.

## Authorization is evaluated on the change

The shape is a function, and it is called again on every evaluation rather than
built once and cached. So a shape that closes over a role, a team membership or a
plan re-reads all three each time it runs. A subscription cannot outlive the
permission that opened it: the moment a row stops matching, the next evaluation
simply does not return it, and the client is told.

This is also why there is no second authorization path here. The shape is a
`Select`; the route that opens the stream is gated the way any route is; and the
policy set is the one Cedar already holds.

## Serving it

```python
from wreath.sync import sync_stream

@app.get("/sync/photos/mine")
async def stream_mine(request):
    subscription = photos.subscribe(request.identity, "mine")
    if subscription is None:
        return Response(b"too many open subscriptions", status=429)
    return sync_stream(subscription, lambda: app.orm().session())
```

The client receives one `snapshot` event, then a `delta` event whenever the
answer moves, and a keepalive comment while it is idle.

Note the session **factory**. A stream may be open for hours and is idle for
almost all of them, so it must not hold a database connection — a hundred open
tabs would exhaust a pool of twenty. It also must not hold a *session*, because
one opened before a write would re-read its own identity map and cheerfully
report that nothing changed.

## Writes go through the front door

There is no client write path here, and that is a design decision rather than an
unfinished edge. Writes go through the ordinary route, where validation, Cedar
and the ORM already live. Read-only sync is a feature with a correct
implementation; two clients writing one field is a merge-semantics problem that
Wreath would then own forever.

## What happens on reconnect

A reconnecting client is sent a fresh `snapshot`, not a resumed delta.

That is correct rather than merely convenient. The snapshot carries the
authoritative key set, so a client that applies it drops everything no longer
present — the tombstone rule applied wholesale. It costs one bounded query,
which is what the bound was for.

What it does not do is tell a client what it missed *without* re-sending the
answer. That needs a row-grained change feed appended inside the writing
transaction, which is [the audit trail](audit-log.md)'s hook rather than one this
module adds on its own. `docs/reference/roadmap.md` carries the row.

## Watching more than one model

An answer can move because something *other* than the model was written — a
membership table that decides which photos are visible, a role table that decides
who may see them:

```python
photos = Sync(Photo, key=lambda row: row.id, watch=[TeamMembership, Role])
```

Forgetting one is the bug where a revocation is only noticed the next time the
row itself happens to change, which may be never.

## Across workers

Pass a message bus and a write taken on any worker wakes the subscriptions held
on every worker:

```python
photos = Sync(Photo, key=lambda row: row.id, bus=app.messaging("bus", database="app"))
```

Without one, the local half still works — which is the right configuration for a
single-worker deployment or a test.

An idle stream emits an SSE comment every `keepalive` seconds — fifteen by
default, and `Sync(..., keepalive=...)` changes it. That tick is not cosmetic:
it is what stops a proxy closing an idle connection, and it is how a client that
went away without saying so is discovered.

The signal that travels is model-grained and means only *something moved, go and
look*. It is never the delta itself. That is what keeps this correct under a
write that arrives out of order, twice, or from a worker you have never heard
from: the answer is always recomputed, never patched.

Reference: [`wreath.sync`](../reference/sync.md)
