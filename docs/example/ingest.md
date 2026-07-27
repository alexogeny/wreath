# Uploads and ingest

A camera-trap network moves far more bytes than rows. One SD card is a few
gigabytes of JPEGs; the database row describing a collection event is a few
hundred bytes. This chapter is about keeping those two apart — the images never
touch the request path, and the work that follows them never blocks a response.

Three moving parts, in the order a field team meets them:

1. The application **signs a URL**. It checks who is asking and what they are
   asking about, once, before any bytes move.
2. The uploader **writes to that URL**. No session, no cookie — the signature is
   the credential.
3. The application **unpacks the card in the background** and the team watches a
   task id.

## Minting a URL

Only a ranger may mint one, and only for a deployment that exists:

```console
$ curl -s -X POST 'localhost:8000/cards?deployment_id=1' -b cookies.txt
```

```json
{
  "deployment_id": 1,
  "key": "cards/olkiramatian/1/SD-0001.zip",
  "url": "/media?key=cards/olkiramatian/1/SD-0001.zip&expires=1785155850&signature=0a3bd85360f6c5b9806ed48b0562081917db54480e2551fddfc1ba2db0f1d603",
  "expires_in": 900,
  "content_type": "application/zip",
  "max_bytes": 67108864
}
```

**Minting is authorised; the URL is not.** That asymmetry is the whole design.
Everything expensive to decide — is this caller a ranger, does this deployment
exist, which reserve owns it — happens here, once. What comes back is a bearer
credential for exactly one write, so the storage service can accept it knowing
nothing about observers, reserves or policies.

Three things are inside the signature, and each one is a separate refusal:

| Edit the URL | What happens |
| --- | --- |
| `expires` moved later | `403` — the deadline is signed, so buying time invalidates it |
| `key` changed | `403` — a URL is good for one object, not for the bucket |
| `signature` altered | `403` — one flipped hex digit is enough |
| method changed to `GET` | `404`/`405` — the route is declared `PUT` only |

The deadline is **absolute**, not a lifetime: `expires_in` is 900 seconds, and
what travels in the URL is the UNIX time it stops working. That is what makes
the check possible with no state kept between minting and writing.

## Writing the bytes

```console
$ curl -s -X PUT --data-binary @SD-0001.zip \
    -H 'content-type: application/zip' \
    'localhost:8000/media?key=cards/olkiramatian/1/SD-0001.zip&expires=1785155850&signature=0a3bd8…'
{"key":"cards/olkiramatian/1/SD-0001.zip","size":514}
```

`201`, and no session was involved. In production this URL points at S3 and the
application never sees the bytes at all — `PUT /media` exists because
`LocalObjectStore` has no service in front of it, so the example serves its own
presigned URLs. It is worth reading for exactly that reason: it is the check S3
performs inside its own infrastructure, written out where you can see it.

Swapping `backend="local"` for `backend="s3"` deletes that one route and changes
nothing else.

!!! note "The key travels in the query string, and that is the router's doing"
    `store.url()` mints `/<key>?expires=…&signature=…`, and a key here is
    `cards/olkiramatian/1/SD-0001.zip` — four segments. Wreath's router has no
    multi-segment path parameter, so no route can bind that path. The mint
    endpoint therefore re-expresses the credential as `?key=`. The signature
    covers the key, the method and the deadline and does not care which part of
    the URL carried them, so nothing is weakened — but it is a rearrangement the
    framework forced rather than a design choice, and the example says so rather
    than presenting it as one.

## Unpacking, in the background

```console
$ curl -s -X POST localhost:8000/cards/1/ingest -b cookies.txt
{"task_id":"1","state":"queued"}
```

The response is a handle, not a result. Unpacking a card is minutes of work and
the request is not going to wait for it — which is what makes this a **durable
job** rather than something awaited inline, or something handed to
`wreath.background`. Background work runs after the response *on this process*;
if the process dies, the work is gone and nobody knows. A field team that has
just spent forty minutes uploading over a satellite link has to be able to lose
the connection, close the laptop, and come back to a finished ingest. That
requires the work to be a row in PostgreSQL.

The **job id is the task id**, so there is one identifier rather than two to
correlate, and it is seeded `queued` at launch — a client that starts polling
immediately sees a pending task rather than a `404` it would read as a failure.

```console
$ curl -s localhost:8000/tasks/1 -b cookies.txt
```

```json
{
  "task_id": "1",
  "percent": 100.0,
  "message": "done",
  "state": "done",
  "error": null
}
```

`GET /tasks/1/stream` is the same progress as server-sent events, and it closes
itself once the task is terminal rather than leaving the connection open for a
client to notice. An ingest that dead-letters is exactly when someone is
watching, and a stream that merely stops producing looks identical to a network
problem.

Afterwards the store holds the archive and its contents, under a layout that can
be listed per reserve or per deployment:

```
cards/olkiramatian/1/SD-0001.zip
images/olkiramatian/1/IMG_0001.JPG
images/olkiramatian/1/IMG_0002.JPG
images/olkiramatian/1/IMG_0003.JPG
```

That layout is a decision. The retention rule in this domain is per-reserve — a
permit expires and everything collected under it has to go — so a layout that
cannot express the rule turns a delete into a full scan.

## Three refusals worth knowing

**Two rangers pressing the button get one job.** `launch(..., key=…)` deduplicates,
and both callers are handed the same task to watch. Two workers unpacking one
archive into one prefix is not a failure the store would report — both writes
succeed and the last one wins — so the key is the only thing preventing it.

**A bad card fails once, not five times.** The handler is registered with
`retries=0`, because every way it fails is a fact about the bytes that were
uploaded: absent, truncated, not a zip, or carrying an entry name the store
refuses. None of those changes on a second attempt, and retrying an unreadable
archive with exponential backoff tells the field team an hour later than it
could have.

**Task ids are job ids, which are a sequence.** Both watch endpoints take an
authorisation predicate, and without one every ingest's state, message and error
text is readable by whoever counts from one.

## What this chapter does not do

**It does not generate thumbnails.** `Sighting.thumbnail_key` exists and the
ingest leaves it null. Deriving a thumbnail means decoding a JPEG, and wreath
ships no image codec — a framework with no mandatory runtime dependencies is not
going to grow one. A real deployment generates them in this same job with Pillow
or an out-of-process `vips` call, writes them to `thumbnail_key`, and changes
nothing else: the column, the key layout and the job are already the right
shape. The example stops where it would otherwise have to pretend.

**`unzip_stream` is not safe for anonymous input**, and the example is arranged
so it never sees any. It reads the archive whole and decompresses each entry
whole, so peak memory is the archive plus its largest entry, and nothing bounds
the expansion ratio. Minting requires a ranger and `MAX_CARD_BYTES` caps what
even a ranger can make a worker allocate — those two together are what make "an
operator's archive" true here rather than aspirational.

## The queue's tables are not in the migration artifact, and you do not apply them

Worth knowing before you build a database for this by hand.
`wreath migrations generate` derives its artifact from the ORM models, and the
job queue is not an ORM model — it is infrastructure the runner owns. So a
database built purely from `example/migrations/` has every table the
application declares and none of the tables its runner needs.

**Wreath creates them itself.** `app.jobs(...)` registers the runner as a schema
component, and startup applies its DDL before any handler runs — see
[`wreath.schema`](../reference/schema.md). The two mechanisms stay separate on
purpose: your artifact describes *your* models, and wreath owns its own
furniture, so neither has to know about the other.

If your database role cannot create relations, that is a first-class path rather
than a wall: `wreath schema sql` prints exactly what a DBA needs to apply, and
`wreath schema check` verifies it landed. Startup then refuses with the missing
relation named, instead of failing at the first launch with
`relation "…jobs" does not exist`.

This example used to export a `queue_schema_sql` so the quickstart, the seeder
and the test fixtures could apply the same statements by hand. That join was a
workaround for a gap in wreath, and it is gone.

## Where to look

| File | What it holds |
| --- | --- |
| `example/camera_trap/media.py` | The key layout, the URL lifetime, and why thumbnails are absent |
| `example/camera_trap/routers/uploads.py` | The four endpoints, and the signature check written out |
| `example/camera_trap/tasks.py` | The ingest handler, and the queue's DDL |
| `tests/example/test_card_uploads.py` | Thirteen tests that mint a URL and then use *that* URL |
