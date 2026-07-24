# Object storage

Blobs live somewhere else — a bucket, a disk, a test double — and every app ends up hand-rolling the same pathlib-over-S3 wrapper, the same presigned-URL dance, the same streaming zip. Wreath ships that layer, zero-dependency, with one interface over every backend.

## One protocol, three backends

Every backend implements the same `Storage` protocol, so code written against it moves from a laptop to S3 without a rewrite:

- `LocalStorage` — a filesystem root, symlink- and `..`-safe via the same `openat` containment as static files; writes are atomic (temp-then-`os.replace`).
- `S3Storage` — the S3 REST API spoken directly over wreath's own [`HTTPClient`](http-client.md), signed with a zero-dep AWS SigV4 implementation. **No boto3.** S3-compatible endpoints (MinIO, R2, Wasabi, Spaces) work through the same code.
- `InMemoryStorage` — the dev/test twin.

Register one on the app and it is lifespan-managed, exposed on `app.state.storage_<name>`:

```python
from wreath import Wreath

app = Wreath()

app.storage("scratch", backend="local", root="./var/blobs")
app.storage("assets", backend="s3", bucket="ev-assets", region="ap-southeast-2")
# credentials from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
```

## Reading and writing

```python
store = app.state.storage_assets

await store.write("reports/q3.csv", body, content_type="text/csv")
data = await store.read("reports/q3.csv")

# Ranged / windowed reads stream without buffering the whole object.
async for chunk in store.read_stream("big.parquet", range=(0, 1 << 20)):
    ...
```

A `StoragePath` gives the `s3path`-style ergonomics — join with `/`, and read/write/glob without threading the store around:

```python
p = store.path("org/acme") / "project" / "state.json"
await p.write_bytes(payload)
if await p.exists():
    async for child in (store.path("reports") ).iterdir():
        ...
```

## Presigned URLs

Hand a client a time-boxed URL and let it talk to the bucket directly — the signature is computed locally, with no network round-trip:

```python
url = store.url("reports/q3.csv", expires=900)          # presigned GET
# or, from a path:
url = p.presigned_url(expires=900, method="PUT")
```

## Uploads land straight in storage

A multipart `File` streams into `write_stream` — the object never fully materialises in memory:

```python
from wreath.binding import File

@app.post("/upload")
async def upload(request, file: File) -> dict:
    stat = await store.write_stream(file.filename, file.chunks(),
                                    content_type=file.content_type)
    return {"key": stat.key, "size": stat.size}
```

## Hero: stream a zip of many objects

The recurring report-export problem — bundle hundreds of objects into a download — without buffering a single one. `zip_stream` emits a valid **Zip64** archive incrementally, CRC computed on the fly:

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

Memory stays flat whether the archive is one object or ten thousand.
