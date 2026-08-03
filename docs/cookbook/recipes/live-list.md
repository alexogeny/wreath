# Keep a client's list live

You have a list a client renders — a user's photos, a team's open tickets, a
dashboard's alerts — and other people can change it. You want the client to be
told what changed, including when a row is taken *away* from it.

## The whole thing

```python
from wreath.sync import Sync, sync_stream

photos = Sync(
    Photo,
    key=lambda row: row.id,
    watch=[TeamMembership],
    bus=app.messaging("bus", database="app"),
)

@photos.shape("mine")
def mine(principal):
    return (
        Photo.select()
        .where(Photo.owner_id == principal.sub)
        .order_by(Photo.taken_at.desc())
        .limit(500)                       # required — see below
    )

@app.get("/sync/photos/mine")
async def stream_mine(request):
    subscription = photos.subscribe(request.identity, "mine")
    if subscription is None:
        return Response(b"too many open subscriptions", status=429)
    return sync_stream(subscription, lambda: app.orm().session())
```

The client:

```js
const held = new Map()
const source = new EventSource("/sync/photos/mine")

source.addEventListener("snapshot", (event) => {
  const { rows, keys } = JSON.parse(event.data)
  held.clear()                                  // the key set is authoritative
  for (const row of rows) held.set(row.key, row.values)
  render(held)
})

source.addEventListener("delta", (event) => {
  const { upserted, removed } = JSON.parse(event.data)
  for (const row of upserted) held.set(row.key, row.values)
  for (const key of removed) held.delete(key)   // <- do not skip this
  render(held)
})
```

## Three things to get right

**The `limit` is mandatory, and `Sync` refuses a shape without one at import.**
It is not a performance hint. It bounds the key set held per open subscription,
and it is what lets a revoked row be an ordinary diff instead of an unbounded
search.

**Handle `removed`, or you have built a leak.** A client that applies `upserted`
and ignores `removed` keeps every row it was ever revoked — including rows it
lost because it was removed from a team. This is the line that makes the feature
correct, and it is one line.

**Pass a session *factory*, not a session.** A stream is open for hours and idle
for most of them. Holding a connection would exhaust the pool; holding a session
would serve stale rows out of its own identity map and report that nothing
changed.

## Watching what actually moves the answer

`watch=[TeamMembership]` above matters more than it looks. If visibility depends
on a membership row, then removing somebody from a team must wake the
subscription — otherwise the revocation is noticed the next time the *photo*
happens to change, which may be never.

## Writes go through the front door

There is no client write path here. Post the change to an ordinary route, where
validation and Cedar already are; the write commits, the subscription wakes, and
the client sees its own change arrive by the same path as everybody else's.

Reference: [`wreath.sync`](../../reference/sync.md) ·
Guide: [Subscribing to a query](../../guides/sync.md)
