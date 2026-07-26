# Object storage

Blobs live somewhere else — a bucket, a disk, a test double — and every app ends up hand-rolling the same pathlib-over-S3 wrapper, the same presigned-URL dance, the same streaming zip. Wreath ships that layer, zero-dependency, with one interface over every backend.

## User story: let the browser upload straight to the bucket

> *As an API author, I don't want large uploads flowing through my API process at
> all. I want to hand the browser a short-lived URL, let it `PUT` directly to the
> bucket, and just record the key — with the signature computed locally, no round
> trip to S3.*

```python
app.storage("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")

@app.post("/uploads")
async def start_upload(request):
    store = app.state.storage_assets
    body = await request.json()
    url = store.url(f"incoming/{body['key']}", expires=900, method="PUT")   # seconds
    return {"upload_url": url}
```

`store.url(...)` signs the URL in-process — there is no network call to mint it.
On S3 it is a fully query-signed URL the client uses directly; on `LocalStorage`
it is a signed relative path you verify at your own route, so the same handler
works in dev. Streaming the bytes through your process instead is the
[upload-into-storage](#uploads-land-straight-in-storage) pattern below.

## One protocol, three backends

Every backend implements the same [`Storage`](../reference/storage.md) protocol, so code written against it moves from a laptop to S3 without a rewrite:

- **`LocalStorage`** — a filesystem root, symlink- and `..`-safe via the same `openat` containment as [static files](static-files.md); writes are atomic (temp-then-`os.replace`, `fsync`'d).
- **`S3Storage`** — the S3 REST API spoken directly over wreath's own [`HTTPClient`](http-client.md), signed with a zero-dep AWS SigV4 implementation. **No boto3.** S3-compatible endpoints (MinIO, R2, Wasabi, Spaces) work through the same code with path-style addressing.
- **`InMemoryStorage`** — the dev/test twin, so your tests never touch a network or a disk.

Every operation goes through one containment gate, `normalize_key`: it rejects absolute keys, `..` traversal, and control characters, and collapses `//`/`.` segments — the single place `s3path`-style code was historically unsafe.

## Wiring it up

Register a backend on the app and it is lifespan-managed, exposed on `app.state.storage_<name>`:

```python
from wreath import Wreath

app = Wreath()

app.storage("scratch", backend="local", root="./var/blobs")
app.storage("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")
# S3 credentials from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
```

```python
store = app.state.storage_assets
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

## `StoragePath` — pathlib over the bucket

A `StoragePath` gives the `s3path` ergonomics without threading the store through your code:

```python
p = store.path("org/trailhead") / "project" / "state.json"
p.name          # "state.json"
p.suffix        # ".json"
p.parent        # StoragePath("org/trailhead/project")

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

`open("wb")` **buffers** and flushes on exit — for true streaming writes call `write_stream` directly.

## Presigned URLs

Hand a client a time-boxed URL and let it talk to the bucket directly — the signature is computed locally, with **no network round-trip**:

```python
url = store.url("reports/q3.csv", expires=900, method="GET")   # seconds
url = p.presigned_url(expires=900, method="PUT")               # from a path
```

On S3 this is a fully query-signed URL the client can use directly. On `LocalStorage` it is a signed **relative** path (`/<key>?expires=…&signature=…`) you verify at your own route with `store.verify_local_url(...)` — so the same code path works in dev.

## Uploads land straight in storage

A multipart upload streams into `write_stream` — bridge the parsed part's bytes with `file_chunks`:

```python
from typing import Annotated
from wreath.binding import File
from wreath.request import UploadedFile
from wreath.storage import file_chunks

@app.post("/upload")
async def upload(request, file: Annotated[UploadedFile, File()]) -> dict:
    stat = await store.write_stream(
        file.filename,
        file_chunks(file.data),
        content_type=file.content_type,
    )
    return {"key": stat.key, "size": stat.size}
```

On S3, `write_stream` automatically switches to a **multipart upload** once the buffered data crosses the part size (≥ 5 MiB parts, 8 MiB default), aborting the upload if any part fails — so large files never fully materialize.

## Hero: stream a zip of many objects

The recurring report-export problem — bundle hundreds of objects into a download — without buffering a single one. `zip_stream` emits a valid **Zip64** archive incrementally, each entry *stored* (no compression) with its CRC computed on the fly:

```python
from wreath.storage import zip_stream
from wreath.response import StreamingResponse

@app.get("/export.zip")
async def export(request):
    keys = [o.key async for o in store.list(prefix="reports/2026/")]
    return StreamingResponse(
        zip_stream(store, keys),
        headers=[(b"content-disposition", b'attachment; filename="export.zip"')],
        media_type=b"application/zip",
    )
```

Memory stays flat whether the archive holds one object or ten thousand. (`unzip_stream` goes the other way, though Phase 1 buffers the archive via stdlib `zipfile`.)

## Gotchas

- **Keys are normalized and contained.** `..`, absolute keys, and control characters raise `StorageError`; `LocalStorage` additionally refuses symlinked path components.
- **S3 part size.** Multipart parts are ≥ 5 MiB (`part_size` default 8 MiB); `read_stream` fetches in a `window` (8 MiB default). Both are tunable on the `S3Storage` backend.
- **MinIO / R2 / other S3-compatible.** The S3 backend supports path-style addressing and a custom host — point it at the endpoint and it just works, same code.
- **Presign expiry** is in seconds and clamped by the provider (SigV4 max is 7 days); the URL embeds its own signing time, so client clock skew doesn't matter.
- **Credentials never leak** — `S3Storage.__repr__` omits them; `AWS_SESSION_TOKEN` is honored for assumed roles.

See also: [HTTP client](http-client.md) (the wire S3 rides on), [static files](static-files.md) (the same `openat` containment), [forms & uploads](forms.md).
