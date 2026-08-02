# Object storage

Blobs live somewhere else — a bucket, a disk, a test double — and every app ends up hand-rolling the same pathlib-over-S3 wrapper, the same presigned-URL dance, the same streaming zip. Wreath ships that layer, zero-dependency, with one interface over every backend.

## User story: let the browser upload straight to the bucket

> *As an API author, I don't want large uploads flowing through my API process at
> all. I want to hand the browser a short-lived URL, let it `PUT` directly to the
> bucket, and just record the key — with the signature computed locally, no round
> trip to S3.*

```python
app.objects("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")

@app.post("/uploads")
async def start_upload(request):
    store = app.state.objects_assets
    body = await request.json()
    url = store.url(f"incoming/{body['key']}", expires=900, method="PUT")   # seconds
    return {"upload_url": url}
```

`store.url(...)` signs the URL in-process — there is no network call to mint it.
On S3 it is a fully query-signed URL the client uses directly; on `LocalObjectStore`
it is a signed relative path you verify at your own route, so the same handler
works in dev. Either way the URL stops working when `expires` runs out — S3 applies
the window itself, and `verify_local_url` checks the deadline before it serves
anything. Streaming the bytes through your process instead is the
[upload-into-storage](#uploads-land-straight-in-storage) pattern below.

## One protocol, three backends

Every backend implements the same [`ObjectStore`](../reference/objects.md) protocol, so code written against it moves from a laptop to S3 without a rewrite:

- **`LocalObjectStore`** — a filesystem root, symlink- and `..`-safe via the same `openat` containment as [static files](static-files.md); writes are atomic (temp-then-`os.replace`, `fsync`'d).
- **`S3ObjectStore`** — the S3 REST API spoken directly over wreath's own [`HTTPClient`](http-client.md), signed with a zero-dep AWS SigV4 implementation. **No boto3.** S3-compatible endpoints (MinIO, R2, Wasabi, Spaces) work through the same code with path-style addressing.
- **`MemoryObjectStore`** — the dev/test twin, so your tests never touch a network or a disk.

Every operation goes through one containment gate, `normalize_key`: it rejects absolute keys, `..` traversal, and control characters, and collapses `//`/`.` segments — the single place `s3path`-style code was historically unsafe.

## Wiring it up

Register a backend on the app and it is lifespan-managed, exposed on `app.state.objects_<name>`:

```python
from wreath import Wreath

app = Wreath()

app.objects("scratch", backend="local", root="./var/blobs")
app.objects("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")
# S3 credentials from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
```

```python
store = app.state.objects_assets
```

## Reading and writing

```python
# small objects: one call each
await store.write("reports/q3.csv", body, content_type="text/csv")
data: bytes = await store.read("reports/q3.csv")

# ranged / windowed reads stream without buffering the whole object
async for chunk in store.read_stream("big.parquet", range=(0, (1 << 20) - 1)):
    ...

# metadata + existence, without downloading the body
meta = await store.stat("reports/q3.csv")        # ObjectStat(key, size, etag, last_modified, content_type)
if await store.exists("reports/q3.csv"):
    ...

# list a prefix (S3 pagination is handled for you); delete
async for obj in store.list(prefix="reports/2026/"):
    print(obj.key, obj.size)
await store.delete("reports/q3.csv")
```

`read_stream`'s `range` is an **inclusive** byte window (`(start, end)`), matching HTTP `Range`. On S3 it is fetched in bounded windows so a multi-gigabyte object never lands in memory.

## `ObjectPath` — pathlib over the bucket

A `ObjectPath` gives the `s3path` ergonomics without threading the store through your code:

```python
p = store.path("org/trailhead") / "project" / "state.json"
p.name          # "state.json"
p.suffix        # ".json"
p.parent        # ObjectPath("org/trailhead/project")

await p.write_bytes(payload)
raw = await p.read_bytes()
if await p.exists():
    await p.unlink()

async for child in (store.path("reports")).iterdir():
    ...
async for csv in (store.path("reports")).glob("2026/*.csv"):
    ...

# streaming read via an async context manager
async with store.path("big.parquet").open("rb") as f:
    async for chunk in f:
        ...
```

**`iterdir` and `glob` are not `pathlib`'s.** An object store has no directories to
stop at, only keys with slashes in them, so `iterdir` yields every key under the
prefix at any depth and `glob` matches `fnmatch`-style over the whole key — `*`
crosses `/`. Under `reports/`, `glob("*.csv")` therefore matches
`reports/2026/q3.csv` as well as `reports/summary.csv`. For one level only, pass
`delimiter="/"` to `store.list(...)`, which S3 honours (the local and memory
backends ignore it).

`open("wb")` **buffers** and flushes when the block ends normally — a block that
raises stores nothing, so a failed handler leaves no truncated object under the
key. For true streaming writes call `write_stream` directly.

## Presigned URLs

Hand a client a time-boxed URL and let it talk to the bucket directly — the signature is computed locally, with **no network round-trip**:

```python
url = store.url("reports/q3.csv", expires=900, method="GET")   # seconds
url = p.presigned_url(expires=900, method="PUT")               # from a path
```

On S3 this is a fully query-signed URL the client can use directly. On `LocalObjectStore` it is a signed **relative** path (`/<key>?expires=…&signature=…`) you verify at your own route with `store.verify_local_url(...)` — so the same code path works in dev. `MemoryObjectStore` mints an unroutable `memory://` URL and verifies it the same way, which is how a presign route gets a test.

The local `expires` in the query is the **absolute UNIX time** the URL dies, not the lifetime you passed; it is covered by the signature, so it cannot be edited, and `verify_local_url` refuses both a bad signature and a passed deadline:

```python
from typing import Annotated

from wreath import Request
from wreath.binding import Query
from wreath.response import Response

@app.get("/blobs/{name}")
async def serve(
    request: Request,
    name: str,
    expires: Annotated[int, Query()],
    signature: Annotated[str, Query()],
) -> Response:
    if not store.verify_local_url(name, method="GET", expires=expires, signature=signature):
        return Response(b"", status=403)
    return Response(await store.read(name))
```

A path parameter captures one segment, so a key with `/` in it needs a route per
level or a key carried in the query string — the URL's path is the key verbatim,
and how you route to it is yours.

## Uploads land straight in storage

A parsed upload streams into `write_stream`. Read it with `UploadedFile.chunks()`, which
covers both kinds of part — wreath spools any part over `spool_max_bytes` (1 MiB by
default) to a temporary file and leaves `.data` **empty**, so `file_chunks(file.data)`
would store a zero-byte object for exactly the uploads big enough to care:

```python
from typing import Annotated

from wreath import Request
from wreath.binding import File
from wreath.request import UploadedFile

async def _body(upload: UploadedFile):
    for chunk in upload.chunks():        # in memory or spooled, both read here
        yield chunk

@app.post("/upload")
async def upload(request: Request, file: Annotated[UploadedFile, File()]) -> dict:
    stat = await store.write_stream(
        f"incoming/{file.filename}",
        _body(file),
        content_type=file.content_type,
    )
    return {"key": stat.key, "size": stat.size}
```

`file.filename` is whatever the client sent. `normalize_key` still contains it — `..`,
absolute keys and control characters raise `ObjectError` rather than escaping — but a
hostile name raises where you may have wanted a 400, so validate it if that matters.
`file_chunks(data)` remains the bridge when you already hold the bytes.

On S3, `write_stream` automatically switches to a **multipart upload** once the buffered
data crosses the part size (≥ 5 MiB parts, 8 MiB default), so large files never fully
materialize. Once that upload exists, any failure — a rejected part, a producer that
raises, a failed completion, a cancellation — aborts it before the exception propagates;
if the abort itself fails, the store's `orphaned_uploads` counter records that parts were
left in the bucket. A bucket taking streamed writes still wants a lifecycle rule expiring
incomplete multipart uploads, because a process killed outright never reaches the abort.

## Resumable uploads: the third option

The two paths above both lose a 4 GB upload to one dropped connection, and they lose it
in opposite ways. A presigned URL never reaches your process, so there is nothing to
resume and nothing to check either — the policy engine never sees the write, the quota
is never consulted, the declared media type is never verified, and you learn the object
exists only if the client says so. Streaming through `write_stream` checks everything
and starts again from zero.

`resumable()` is the third: the application stays in the way of the **decisions** while
the bytes arrive in restartable pieces.

```python
from wreath.objects import ObjectUploadStore, UploadLimits, resumable

uploads = resumable(
    store,
    uploads=ObjectUploadStore(store),        # shared across workers; see below
    limits=UploadLimits(max_size=5 * 1024**3),
    expire=hours(24),
    jobs=runner,
    on_complete="process_photo",             # a registered task, not a callback
)
app.include_router(uploads.router("/uploads", permissions=["uploads::write"]))
```

`permissions=` is the router's ordinary keyword, so authorization here is the same
declaration as on any other route — which is precisely what a presigned URL gives up.

### Which one to use

| Use | When |
| --- | --- |
| `store.url(...)` — presigned | The object is opaque to you and the client is trusted. Nothing flows through your process, so nothing is checked. |
| `write_stream` — through the app | Small uploads, where a restart costs nothing. |
| `resumable(...)` | Size, media type, or a quota must be enforced — or the network is bad enough that starting over is not acceptable. |

Bytes now flow through your process, which is bandwidth the presigned path avoided.
That is the trade, and it is a real one; it buys admission control and resumption.

### The protocol

The shipped surface is [Resumable Uploads for HTTP](https://datatracker.ietf.org/doc/html/draft-ietf-httpbis-resumable-upload),
`draft-ietf-httpbis-resumable-upload-12`. **tus 1.0.x is deliberately not served** — the
two vocabularies overlap enough that one implementation answering both would have to
guess which dialect a request is speaking from the headers it did *not* send.

```http
POST /uploads                     Upload-Complete: ?0        → 201 + Location
HEAD /uploads/{id}                                           → 204 + Upload-Offset
PATCH /uploads/{id}               Content-Type: application/partial-upload
                                  Upload-Offset: 8388608
                                  Upload-Complete: ?1        → 204
DELETE /uploads/{id}                                         → 204
```

A client that dropped mid-upload sends `HEAD`, reads `Upload-Offset`, and resumes from
there. **The client's offset is a claim, never an authority** — it is compared against
what the server durably holds, and a mismatch is a `409` carrying the real one.

### Admission happens before the first byte

A declared `Upload-Length` over `max_size`, or refused by your `quota` predicate, is a
`413` against a request that has sent nothing:

```python
async def quota(request, declared_bytes):
    return await tenant_has_room(request.identity, declared_bytes)

uploads = resumable(store, quota=quota, limits=UploadLimits(max_size=5 * 1024**3))
```

The declared length is also enforced *as bytes arrive*, because `Content-Length` is a
claim and a chunked body has none — an upload cannot creep past its declared size one
chunk at a time.

Content type is decided on the **sniffed** leading bytes, not the client's claim. A body
declaring `image/png` that is actually HTML is refused with `415` rather than stored and
later served back from your origin. `sniff_content_type` recognises only unambiguous
magic numbers; it is there to catch a lie, not to classify every format.

### Completion is a job, not a callback

`on_complete` names a task registered on your `JobRunner` and is enqueued with the
upload id as its idempotency key. A callback awaited inside the request is lost by the
next deploy — which is the failure every hand-rolled upload pipeline eventually finds.
It is also the seam that keeps image processing out of wreath: thumbnailing,
transcoding and scanning are your task's business, and the pixels stay yours.

### Where in-progress state lives

`MemoryUploadStore` is the default and holds state in **one worker's memory**, so a
resume that lands on a sibling worker is a `404` and the client starts over. That fails
closed rather than corrupting anything, but it is the wrong choice behind a
non-sticky load balancer. `ObjectUploadStore(store)` keeps the record in the same bucket
every worker already talks to — shared state with no second datastore, at one extra
round trip per request.

### The 5 MiB floor, advertised rather than hidden

Every response carries `Upload-Limit`, so a client can size its chunks before it starts:

```
Upload-Limit: max-size=5368709120, min-append-size=8388608, max-age=86400
```

`min-append-size` appears when the backend has a floor. S3 assembles with its own
multipart upload — which is why completion transfers nothing — and S3 refuses a
non-final part under 5 MiB (`S3ObjectStore`'s own default part size is 8 MiB). The
alternative would be staging the remainder and re-uploading it on the next call: a
silent write amplification on exactly the slow network the protocol exists to survive.
The append that *completes* an upload is exempt, as the last part is. Other backends
have no floor and advertise none.

Set `max_append_bytes=` to your app's `RequestLimits.max_body_bytes` so the advertised
ceiling is one your application will actually read.

### Sweeping what nobody finished

An abandoned S3 multipart upload is billed for its parts until something aborts it.
`sweep()` is part of the feature, not a follow-up:

```python
@runner.schedule("*/15 * * * *")
async def reap_uploads() -> None:
    await uploads.sweep()
```

`swept_uploads` counts what it reclaimed and `aborted_uploads` counts what it could not
— a non-zero second number means storage is being billed for parts belonging to no
object.

## Hero: stream a zip of many objects

The recurring report-export problem — bundle hundreds of objects into a download — without buffering a single one. `zip_stream` emits a valid **Zip64** archive incrementally, each entry *stored* (no compression) with its CRC computed on the fly:

```python
from wreath.objects import zip_stream
from wreath.response import StreamingResponse

@app.get("/export.zip")
async def export(request):
    keys = [o.key async for o in store.list(prefix="reports/2026/")]
    return StreamingResponse(
        zip_stream(store, keys),
        headers=[
            (b"content-type", b"application/zip"),
            (b"content-disposition", b'attachment; filename="export.zip"'),
        ],
    )
```

`StreamingResponse` invents no headers — there is no `media_type` parameter — so the
`content-type` goes in the header list with everything else.

Memory stays flat whether the archive holds one object or ten thousand. A key that turns
out to be missing raises *mid-stream*, after bytes have already gone out, so the client
receives a truncated archive; check the keys first if that matters.

`unzip_stream` goes the other way and is **not** its equal in memory: a zip's directory
lives at the end of the file, so the stdlib `zipfile` reader needs a seekable archive and
each entry is decompressed whole. `ZipExtractionLimits` bounds archive bytes, entry count,
one expanded entry, and cumulative expanded bytes. Conservative defaults apply; pass an
explicit budget when the application has a narrower or deliberately larger workload:

```python
from wreath.objects import ZipExtractionLimits, unzip_stream

written = await unzip_stream(
    store,
    "uploads/card.zip",
    limits=ZipExtractionLimits(
        max_archive_bytes=8 * 1024 * 1024,
        max_entries=512,
        max_entry_bytes=4 * 1024 * 1024,
        max_total_bytes=32 * 1024 * 1024,
    ),
)
```

The archive is refused while it is being buffered, and all names and declared expanded
sizes are checked before the first output is written. A refusal therefore leaves no
partially extracted prefix behind.

## Gotchas

- **Keys are normalized and contained.** `..`, absolute keys, and control characters raise `ObjectError`; `LocalObjectStore` additionally refuses symlinked path components.
- **S3 part size.** Multipart parts are ≥ 5 MiB (`part_size` default 8 MiB); `read_stream` fetches in a `window` (8 MiB default). Both are tunable on the `S3ObjectStore` backend.
- **MinIO / R2 / other S3-compatible.** The S3 backend supports path-style addressing and a custom host — point it at the endpoint and it just works, same code.
- **Presign expiry** is in seconds from now on every backend, and enforced on every backend — but by different machinery. S3 embeds its signing time in `X-Amz-Date` and AWS applies the window (max 7 days, rejected at use time). `LocalObjectStore` and `MemoryObjectStore` embed the resulting **deadline** in the signed query, and `verify_local_url` compares it against *this process's* clock. Neither depends on the client's clock. Rotating `url_secret` still invalidates every outstanding local URL at once.
- **`url_secret` is a local/memory setting.** `S3ObjectStore` signs with your AWS credentials and raises `TypeError` if you pass one, rather than accepting a security-shaped argument it would ignore.
- **Etags are unquoted everywhere, and only comparable within one backend.** Memory hashes the content (MD5), local uses an `mtime-size` pair — cheap, not a content hash — and S3 returns whatever it stores. The HTTP header's quotes are added by whoever emits the header.
- **`content_type` reaches S3 but not the disk.** The S3 backend sends and signs it on the `PUT` (or the multipart initiate), so the object is stored with that type; `LocalObjectStore` has nowhere to persist it and guesses from the key's extension on the way back out. Nothing is guessed for S3.
- **A local write's leftovers are not objects.** A hard kill can leave `.<name>.<hex>.tmp` beside the target; `list` skips exactly that shape, so an object you genuinely named `notes.tmp` is still listed. The rename is atomic but not durable — the file is `fsync`'d, its directory is not.
- **Credentials never leak** — `S3ObjectStore.__repr__` omits them; `AWS_SESSION_TOKEN` is honored for assumed roles.
- **A resumable upload's `Upload-Offset` is checked, not believed.** A client claiming an offset it does not hold gets a `409` carrying the real one, and cannot reach across into another upload's parts. The advance is conditional, so two appends racing on one upload lose one rather than leaving a hole in the object.
- **`MemoryUploadStore` is per-worker.** The default confines an upload to the worker that created it; a resume elsewhere is a `404`. Pass `ObjectUploadStore(store)` behind more than one worker.
- **A non-S3 backend pays one extra copy at completion.** The generic assembly writes one object per append and concatenates them by streaming, so memory stays bounded but the bytes are read and written once more. S3 avoids it entirely by assembling in the bucket.
- **`write` takes any buffer, and keeps none of it.** `bytes`, `bytearray` and `memoryview` are all accepted, and no backend holds a reference past the `await` — so a buffer you assembled an object in is yours again the moment the call returns. Assembling in a `bytearray` and handing it over directly is the intended shape; freezing it with `bytes(...)` first is a `memcpy` nothing downstream asked for (390 µs on an 8 MiB resumable append, which was about half of what the append cost). If you write your own backend, honour the same promise: copy inside `write` if you need to retain the bytes.
- **`sweep()` is not scheduled for you.** Nothing reclaims an abandoned upload until you call it, and on S3 the parts are billed in the meantime.

See also: [HTTP client](http-client.md) (the wire S3 rides on), [static files](static-files.md) (the same `openat` containment), [forms & uploads](forms.md).
